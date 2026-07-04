"""
P1c — Fast-path matchday freshness propagation.

Background
----------
The fast workflow (live-matchday.yml every ~10 min) runs
`scripts/live/run_live_update.py`, which kicks `scripts/03_simulate.py`,
which calls `get_team_elo_adjustment()` in `apply_matchday_adjustments`.

The slow workflow (matchday-intel-slow.yml every 3h) maintains
`dashboard/matchday_intelligence.json` — the consolidated state that
embeds per-subsystem freshness warnings (referee/suspensions/player_stats).

Before this round the fast path was BLIND to the slow path's freshness
state: `get_team_elo_adjustment()` returns a float; the
`degradation_warnings` collected in `_ensure_state()` never reached
`live_state.json`. Result: if the slow workflow stalled >6h, the
dashboard's freshness pill never lit — the fast tick silently applied
stale matchday Elo.

This file pins the three failure modes the helper closes:

  1. `matchday_consolidated_missing`  — dashboard JSON absent.
  2. `matchday_consolidated_stale`    — dashboard JSON >6h older than
                                        results_2026.json mtime.
  3. `matchday_subsystem_stale`       — dashboard JSON itself current
                                        but contains a `subsystem_stale`
                                        warning from a producer underneath.

And the negative case:

  4. All fresh + zero embedded freshness warnings → empty list (no
     spurious warnings on a clean tick).

Plus the orchestrator's safe-wrapper:

  5. `_matchday_freshness_warnings_safe()` in run_live_update.py: a
     raised exception inside the helper degrades to a single
     `matchday_freshness_check_error` warning — never propagates.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT / "scripts"), str(ROOT / "scripts" / "live")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _now_utc() -> "datetime":
    from datetime import datetime, timezone  # local import to keep top tidy
    return datetime.now(timezone.utc)


def _make_consolidated_blob(degradation_warnings=None, generated_at: str | None = None) -> dict:
    """Minimal matchday_intelligence.json shape — just enough for the
    helper's parser. Real schema has many more keys; the helper only
    touches `degradation_warnings`.

    R9 P5 B1 update: `generated_at` is now consulted by the freshness
    helper (preferred over mtime). Default to now-ish so tests don't
    appear "ancient" to the content-timestamp path; tests that need
    a controlled age pass an explicit ISO string."""
    return {
        "generated_at": generated_at or _now_utc().isoformat(),
        "schema_version": 1,
        "active_adjustments": [],
        "summary": {},
        "warnings": [],
        "degradation_warnings": degradation_warnings or [],
    }


def _touch_age(path: Path, hours_old: float) -> None:
    """Set the file's mtime to `hours_old` hours BEFORE now. R9 P5 B1:
    also rewrite the JSON content's `generated_at` (if present) to the
    same age, so the content-preferring freshness helper sees a stale
    file the same way the legacy mtime helper did."""
    from datetime import timedelta
    now = os.path.getmtime(path)
    target = now - (hours_old * 3600.0)
    os.utime(path, (target, target))
    # Also update content timestamp if file is JSON with a known ts field.
    try:
        d = json.loads(path.read_text())
        if isinstance(d, dict):
            new_ts = (_now_utc() - timedelta(hours=hours_old)).isoformat()
            updated = False
            for key in ("generated_at", "updated_at"):
                if key in d:
                    d[key] = new_ts
                    updated = True
            if updated:
                path.write_text(json.dumps(d))
                # Re-apply mtime since write_text resets it.
                os.utime(path, (target, target))
    except (json.JSONDecodeError, OSError):
        pass


def _set_relative_age(file_path: Path, ref_path: Path, hours_older: float) -> None:
    """Set `file_path` to be `hours_older` hours OLDER than `ref_path`.
    R9 P5 B1: synchronises BOTH mtime AND the content `generated_at`/
    `updated_at` field so the content-preferring freshness helper sees
    the intended relative age. Both files must already exist."""
    from datetime import datetime, timedelta, timezone
    # Reference timestamp: prefer content updated_at/generated_at, fall
    # back to mtime. This mirrors the new freshness-helper semantics.
    ref_ts = None
    try:
        ref_d = json.loads(ref_path.read_text())
        if isinstance(ref_d, dict):
            for key in ("generated_at", "updated_at", "last_updated_utc", "last_updated"):
                v = ref_d.get(key)
                if isinstance(v, str) and v:
                    try:
                        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        ref_ts = dt
                        break
                    except ValueError:
                        continue
    except (json.JSONDecodeError, OSError):
        pass
    if ref_ts is None:
        ref_ts = datetime.fromtimestamp(os.path.getmtime(ref_path), tz=timezone.utc)
    target_ts = ref_ts - timedelta(hours=hours_older)
    target_epoch = target_ts.timestamp()
    os.utime(file_path, (target_epoch, target_epoch))
    # Update content `generated_at`/`updated_at` to the target time too.
    try:
        d = json.loads(file_path.read_text())
        if isinstance(d, dict):
            updated = False
            for key in ("generated_at", "updated_at"):
                if key in d:
                    d[key] = target_ts.isoformat()
                    updated = True
            if updated:
                file_path.write_text(json.dumps(d))
                os.utime(file_path, (target_epoch, target_epoch))
    except (json.JSONDecodeError, OSError):
        pass


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Monkeypatch OUT_PATH and LIVE in apply_matchday_adjustments to
    point into tmp_path so each test owns its filesystem state."""
    # Re-import to get the current module object (not a cached spec).
    mod = importlib.import_module("apply_matchday_adjustments")
    dash_dir = tmp_path / "dashboard"
    live_dir = tmp_path / "data" / "live"
    dash_dir.mkdir(parents=True)
    live_dir.mkdir(parents=True)
    monkeypatch.setattr(mod, "OUT_PATH", dash_dir / "matchday_intelligence.json")
    monkeypatch.setattr(mod, "LIVE", live_dir)
    monkeypatch.setattr(mod, "DASH", dash_dir)
    return mod, dash_dir, live_dir


# ------------------------------------------------------- mode 1: missing
def test_consolidated_missing_emits_warning(isolated_paths) -> None:
    """No matchday_intelligence.json on disk → loud warning, helper does
    NOT crash. This is the bootstrap-state failure mode (slow workflow
    has never run on this host)."""
    mod, dash_dir, live_dir = isolated_paths
    # results_2026.json exists; matchday_intelligence.json does NOT.
    (live_dir / "results_2026.json").write_text(json.dumps({"completed_matches": []}))
    warnings = mod.get_matchday_freshness_warnings()
    assert len(warnings) == 1
    w = warnings[0]
    assert w["type"] == "matchday_consolidated_missing"
    assert "not present" in w["message"]
    assert "matchday-intel-slow" in w["message"]


# ------------------------------------------------------- mode 2: stale
def test_consolidated_stale_emits_warning(isolated_paths) -> None:
    """matchday_intelligence.json mtime >6h older than results_2026.json
    → matchday_consolidated_stale warning with the age delta."""
    mod, dash_dir, live_dir = isolated_paths
    out_path = dash_dir / "matchday_intelligence.json"
    out_path.write_text(json.dumps(_make_consolidated_blob()))
    results_path = live_dir / "results_2026.json"
    results_path.write_text(json.dumps({"completed_matches": []}))
    # Age the matchday file 7h relative to results (threshold = 6h).
    _set_relative_age(out_path, results_path, hours_older=7.0)
    warnings = mod.get_matchday_freshness_warnings()
    types = [w["type"] for w in warnings]
    assert "matchday_consolidated_stale" in types, (
        f"expected matchday_consolidated_stale, got {types}"
    )
    stale = next(w for w in warnings if w["type"] == "matchday_consolidated_stale")
    # Message includes the age delta and threshold.
    assert "7." in stale["message"] or "older than" in stale["message"]
    assert "6.0h" in stale["message"]


def test_consolidated_fresh_within_threshold_no_stale_warning(isolated_paths) -> None:
    """matchday_intelligence.json mtime within 6h of results_2026.json
    → NO matchday_consolidated_stale warning. Pins the negative
    case — we don't want false alarms on a healthy tick."""
    mod, dash_dir, live_dir = isolated_paths
    out_path = dash_dir / "matchday_intelligence.json"
    out_path.write_text(json.dumps(_make_consolidated_blob()))
    results_path = live_dir / "results_2026.json"
    results_path.write_text(json.dumps({"completed_matches": []}))
    # Age the matchday file 3h relative to results — well within 6h.
    _set_relative_age(out_path, results_path, hours_older=3.0)
    warnings = mod.get_matchday_freshness_warnings()
    types = [w["type"] for w in warnings]
    assert "matchday_consolidated_stale" not in types
    assert "matchday_consolidated_missing" not in types


# ------------------------------------------ mode 3: subsystem-level stale
def test_subsystem_stale_warning_in_consolidated_propagates(isolated_paths) -> None:
    """matchday_intelligence.json is itself FRESH but contains a
    subsystem_stale warning (e.g. referee_2026.json stale) →
    matchday_subsystem_stale warning surfacing the stalled subsystem
    name(s)."""
    mod, dash_dir, live_dir = isolated_paths
    out_path = dash_dir / "matchday_intelligence.json"
    embedded = [
        {
            "subsystem": "referee",
            "scope": "freshness",
            "record_id": "file=referee_2026.json",
            "exception_class": "Stale",
            "message": "referee input is 8h older than results",
            "ts": "2026-06-17T12:00:00+00:00",
        },
        {
            "subsystem": "suspension",
            "scope": "freshness",
            "record_id": "file=suspensions_2026.json",
            "exception_class": "Stale",
            "message": "suspension input is 9h older than results",
            "ts": "2026-06-17T12:00:00+00:00",
        },
    ]
    out_path.write_text(json.dumps(_make_consolidated_blob(embedded)))
    results_path = live_dir / "results_2026.json"
    results_path.write_text(json.dumps({"completed_matches": []}))
    _set_relative_age(out_path, results_path, hours_older=1.0)  # still fresh
    warnings = mod.get_matchday_freshness_warnings()
    types = [w["type"] for w in warnings]
    assert "matchday_subsystem_stale" in types, (
        f"expected matchday_subsystem_stale, got {types}"
    )
    sub_w = next(w for w in warnings if w["type"] == "matchday_subsystem_stale")
    assert "referee" in sub_w["subsystems"]
    assert "suspension" in sub_w["subsystems"]
    assert "matchday-intel-slow" in sub_w["message"]


def test_subsystem_non_freshness_warnings_do_not_propagate(isolated_paths) -> None:
    """A degradation_warnings entry with scope != 'freshness' and
    exception_class != 'Stale' is NOT a freshness signal — must NOT show
    up as matchday_subsystem_stale (those are per-record skips, not
    producer stalls).
    """
    mod, dash_dir, live_dir = isolated_paths
    out_path = dash_dir / "matchday_intelligence.json"
    embedded = [
        # Per-record degradation, not freshness.
        {
            "subsystem": "injury",
            "scope": "record",
            "record_id": "player=Pedri",
            "exception_class": "KeyError",
            "message": "injury record missing 'team' field",
            "ts": "2026-06-17T12:00:00+00:00",
        },
    ]
    out_path.write_text(json.dumps(_make_consolidated_blob(embedded)))
    results_path = live_dir / "results_2026.json"
    results_path.write_text(json.dumps({"completed_matches": []}))
    _set_relative_age(out_path, results_path, hours_older=1.0)
    warnings = mod.get_matchday_freshness_warnings()
    types = [w["type"] for w in warnings]
    assert "matchday_subsystem_stale" not in types
    assert "matchday_consolidated_stale" not in types


# ------------------------------------------------------- mode 4: clean
def test_all_fresh_zero_warnings(isolated_paths) -> None:
    """Healthy tick: consolidated file present, recently updated, no
    embedded freshness warnings → helper returns []."""
    mod, dash_dir, live_dir = isolated_paths
    out_path = dash_dir / "matchday_intelligence.json"
    out_path.write_text(json.dumps(_make_consolidated_blob()))
    results_path = live_dir / "results_2026.json"
    results_path.write_text(json.dumps({"completed_matches": []}))
    _set_relative_age(out_path, results_path, hours_older=0.5)
    assert mod.get_matchday_freshness_warnings() == []


# ----------------------------------------------------------- helper safety
def test_helper_never_raises_on_unparseable_json(isolated_paths) -> None:
    """A truncated/corrupt matchday_intelligence.json must NOT crash the
    helper — it surfaces a `matchday_consolidated_unparseable` warning
    instead. Pins the "freshness probe is a probe, not a tripwire" rule."""
    mod, dash_dir, live_dir = isolated_paths
    out_path = dash_dir / "matchday_intelligence.json"
    out_path.write_text("{not-json")
    results_path = live_dir / "results_2026.json"
    results_path.write_text(json.dumps({"completed_matches": []}))
    _set_relative_age(out_path, results_path, hours_older=1.0)
    warnings = mod.get_matchday_freshness_warnings()
    types = [w["type"] for w in warnings]
    assert "matchday_consolidated_unparseable" in types


def test_helper_returns_list_not_none() -> None:
    """Contract: helper ALWAYS returns a list (never None). Pinning this
    so callers can do `warns = warns + helper()` without a None-guard."""
    # Re-import to ensure we test the live module.
    import apply_matchday_adjustments as mod
    result = mod.get_matchday_freshness_warnings()
    assert isinstance(result, list), (
        f"helper must return list, got {type(result).__name__}"
    )


# ------------------------------------------------- orchestrator safe-wrap
def test_orchestrator_safe_wrapper_returns_list_on_helper_crash(monkeypatch) -> None:
    """`_matchday_freshness_warnings_safe()` in run_live_update.py must
    swallow any Exception from the helper and return a one-element
    `matchday_freshness_check_error` list rather than propagate.

    Pinning this so a future helper bug never takes down a tick."""
    # Patch the helper to raise.
    import apply_matchday_adjustments as mod

    def _boom() -> list[dict]:
        raise RuntimeError("simulated probe failure")

    monkeypatch.setattr(mod, "get_matchday_freshness_warnings", _boom)
    # Import the orchestrator function.
    import run_live_update as rlu
    result = rlu._matchday_freshness_warnings_safe()
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["type"] == "matchday_freshness_check_error"
    assert "simulated probe failure" in result[0]["message"]


def test_orchestrator_safe_wrapper_passes_through_normal_warnings(monkeypatch) -> None:
    """When the helper returns a non-trivial list, the safe-wrapper
    must pass it through unchanged (no double-wrapping, no filtering)."""
    import apply_matchday_adjustments as mod
    fake = [
        {"type": "matchday_consolidated_stale", "message": "test"},
        {"type": "matchday_subsystem_stale", "message": "x",
         "subsystems": ["referee"]},
    ]
    monkeypatch.setattr(mod, "get_matchday_freshness_warnings", lambda: fake)
    import run_live_update as rlu
    result = rlu._matchday_freshness_warnings_safe()
    assert result == fake


# ─────────────────────────────────────── early-exit propagation
# Static check (independent monitor surfaced this gap): every early-exit
# guard in main() must merge `mf_warnings` into its isolated warning
# array. Without these assertions, a refactor could silently regress one
# of the three guards back to the pre-P1c blind state.
def _read_source() -> str:
    return (ROOT / "scripts" / "live" / "run_live_update.py").read_text()


def test_mf_warnings_probed_at_top_of_main() -> None:
    """The freshness probe must run ONCE at the top of main() so all
    three early-exit guards see its result without re-probing."""
    src = _read_source()
    # Probe call appears before circuit-breaker state is interpreted.
    cb_idx = src.find("cb_open = failures >= CB_THRESHOLD")
    # R6 M2: the mf_warnings assignment now folds in _provider_fallback_warnings()
    # alongside _matchday_freshness_warnings_safe(), so the line may be split
    # across multiple lines. Assert on the function call presence + ordering
    # rather than on a one-line assignment literal.
    probe_idx = src.find("_matchday_freshness_warnings_safe()")
    assert probe_idx > 0, "freshness probe call missing"
    assert cb_idx > 0, "circuit-breaker open-state assignment missing"
    assert probe_idx < cb_idx, (
        "freshness probe must precede the circuit-breaker state guard so "
        "early-exit paths can fold its result into their warning arrays."
    )


def test_circuit_breaker_exit_merges_mf_warnings() -> None:
    """Circuit-breaker exit must merge mf_warnings into its warnings arg."""
    src = _read_source()
    # Find the CB block.
    cb_block_start = src.find('"type": "circuit_breaker"')
    assert cb_block_start > 0
    cb_block_end = src.find("return 2", cb_block_start)
    cb_block = src[cb_block_start:cb_block_end]
    assert "mf_warnings" in cb_block, (
        "circuit_breaker exit drops mf_warnings — matchday freshness "
        "won't reach live_state.json on CB-tripped ticks"
    )


def test_circuit_breaker_open_runs_recovery_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tripped breaker must pause betting but still run a protected probe.

    Pre-fix, `failures >= CB_THRESHOLD` returned before fetch_results ran.
    The workflow converted rc=2 to success and committed a fresh breaker
    warning every tick, leaving production permanently paused until manual
    deletion. A clean probe now reaches fetch/hash and resets CB to 0.
    """
    import run_live_update as rlu

    live = tmp_path / "live"
    proc = tmp_path / "processed"
    dash = tmp_path / "dashboard"
    for d in (live, proc, dash):
        d.mkdir()
    monkeypatch.setattr(rlu, "LIVE", live)
    monkeypatch.setattr(rlu, "PROC", proc)
    monkeypatch.setattr(rlu, "DASH", dash)
    monkeypatch.setattr(rlu, "CB_PATH", live / "circuit_breaker_state.json")
    rlu.write_circuit_breaker(rlu.CB_THRESHOLD)

    (live / "results_2026.json").write_text(json.dumps({
        "completed_matches": [],
        "in_play": [],
        "warnings": [],
        "updated_at": "2026-07-04T11:00:00+00:00",
    }))
    (proc / "predictions_live.json").write_text(json.dumps({
        "team_predictions": [],
        "input_hash": rlu.compute_input_hash(),
        "completed_matches": [],
    }))

    calls: list[list[str]] = []

    def fake_run(cmd):
        calls.append(cmd)
        if any("fetch_results.py" in part for part in cmd):
            (live / "results_2026.json").write_text(json.dumps({
                "completed_matches": [],
                "in_play": [],
                "warnings": [],
                "updated_at": "2026-07-04T11:10:00+00:00",
            }))
        return 0

    monkeypatch.setattr(rlu, "run", fake_run)
    monkeypatch.setattr(rlu, "_matchday_freshness_warnings_safe", lambda: [])
    monkeypatch.setattr(rlu, "_provider_fallback_warnings", lambda: [])
    monkeypatch.setattr(sys, "argv", ["run_live_update.py"])

    assert rlu.main() == 0
    assert any(any("fetch_results.py" in part for part in cmd) for cmd in calls), (
        "open breaker returned before fetch_results; recovery probe is dead"
    )
    cb = json.loads((live / "circuit_breaker_state.json").read_text())
    assert cb["consecutive_failures"] == 0
    state = json.loads((dash / "live_state.json").read_text())
    assert state["warnings"] == []


def test_circuit_breaker_warning_has_no_runner_delete_path() -> None:
    """Operator warning must not tell users to delete a GitHub-runner path."""
    import run_live_update as rlu

    w = rlu.circuit_breaker_warning(3, recovery_probe=True)
    msg = w["message"]
    assert "/home/runner/" not in msg
    assert "deleting" not in msg.lower()
    assert w["recovery_probe"] is True


def test_fetch_failure_exit_merges_mf_warnings() -> None:
    """Fetch-failure exit must merge mf_warnings into its warnings arg."""
    src = _read_source()
    block_start = src.find('"type": "fetch_failure"')
    assert block_start > 0
    block_end = src.find("return 0", block_start)
    block = src[block_start:block_end]
    assert "mf_warnings" in block, (
        "fetch_failure exit drops mf_warnings — operator sees fetch fail "
        "but no matchday-staleness signal on the same tick"
    )


def test_input_corruption_exit_merges_mf_warnings() -> None:
    """Input-corruption exit must merge mf_warnings into its warnings arg."""
    src = _read_source()
    block_start = src.find('"type": "input_corruption"')
    assert block_start > 0
    block_end = src.find("return 2", block_start)
    block = src[block_start:block_end]
    assert "mf_warnings" in block, (
        "input_corruption exit drops mf_warnings — matchday-staleness "
        "signal lost when results_2026.json is malformed"
    )


# ─────────────────────────────────── H2 (R2 round 3): crash-handler propagation
# Audit found that the outermost `except Exception` at the module
# entrypoint used isolated warnings (orchestrator_crash only) — the
# `mf_warnings` variable computed inside main() was out of scope when
# main() raised, so the crash handler silently dropped freshness. Fix
# was to RE-PROBE freshness inside the crash handler via the safe wrapper.
def test_h2_crash_handler_probes_freshness_via_safe_wrapper() -> None:
    """The outermost try/except in `if __name__ == '__main__':` must call
    `_matchday_freshness_warnings_safe()` (the safe wrapper, not the raw
    helper) so that even when main() crashes, the freshness signal lands
    on live_state.json alongside the orchestrator_crash warning.
    """
    src = _read_source()
    # Locate the orchestrator-crash block: everything from the FATAL
    # marker to sys.exit(1) at file end.
    crash_idx = src.find("[run_live_update] FATAL")
    assert crash_idx > 0, "orchestrator-crash handler missing"
    crash_block = src[crash_idx:]
    assert "_matchday_freshness_warnings_safe()" in crash_block, (
        "crash handler does NOT probe matchday freshness — when main() "
        "raises, the freshness signal is silently dropped (audit H2). "
        "Add `_matchday_freshness_warnings_safe()` to the warning array "
        "BEFORE write_live_state."
    )


def test_h2_crash_handler_appends_freshness_to_crash_warning() -> None:
    """In the crash handler, the `crash_warnings` list must include BOTH
    the orchestrator_crash entry AND the matchday freshness entries —
    not one or the other. A regression that overwrites instead of
    appending would silently drop one signal.
    """
    src = _read_source()
    crash_idx = src.find("[run_live_update] FATAL")
    crash_block = src[crash_idx:]
    # The crash_warnings list must be appended to (`.extend(`), not
    # overwritten. We assert the contract by checking that BOTH the
    # crash dict literal and `_matchday_freshness_warnings_safe()` appear,
    # AND the freshness call follows the crash literal (so a later append
    # adds to the same list).
    crash_dict_idx = crash_block.find('"type": "orchestrator_crash"')
    freshness_idx = crash_block.find("_matchday_freshness_warnings_safe()")
    assert crash_dict_idx > 0
    assert freshness_idx > 0
    assert freshness_idx > crash_dict_idx, (
        "freshness probe must come AFTER the crash-warning literal so it "
        "extends the same crash_warnings list (not replaces it)."
    )
    # And the call to write_live_state inside the crash block must pass
    # `crash_warnings` as the warnings argument — not a one-shot literal
    # that drops the freshness signal. A simple substring scan is enough
    # since the variable name is unique in that scope.
    assert "warnings=crash_warnings" in crash_block, (
        f"crash handler's write_live_state must use warnings=crash_warnings "
        f"so the merged list (crash entry + freshness entries) reaches "
        f"live_state.json. Crash block:\n{crash_block[:600]}"
    )


# ────────────────────────────────────── R5 C6: fast-path event enrichment
# Audit found that the fast (10-min) workflow's fetch_results call did NOT
# pass --with-events. Card events from in-play matches that lock during a
# fast tick stayed null in results_2026.json until the next slow (3h) tick,
# meaning suspension_tracker could not see them for up to 3h. With R32
# starting 2026-06-28 the suspension data must be timely; a player who
# picks up his 2nd yellow in a fast-tick-locked match must be in
# suspensions_2026.json before the next opponent's win-prob calculation.
def test_r5_c6_fast_path_fetch_results_uses_with_events() -> None:
    """The orchestrator's fetch_results invocation at the top of main()
    must pass --with-events so card events land in results_2026.json on
    the SAME tick the match locks (not 3h later via the slow workflow)."""
    src = _read_source()
    # Locate the fetch_cmd construction.
    fc_idx = src.find('fetch_cmd = [sys.executable, "scripts/live/fetch_results.py"')
    assert fc_idx > 0, "fetch_cmd construction not found in run_live_update.main()"
    # Take a small window after the assignment to inspect the literal.
    fc_block = src[fc_idx:fc_idx + 200]
    assert "--with-events" in fc_block, (
        "fast-path fetch_cmd missing --with-events flag — card events from "
        "matches that lock during a fast tick won't reach suspension_tracker "
        "until the next slow (3h) tick. R5 audit C6."
    )


# ────────────────────────────────────── R5 C4: per-record degradation rollup
# Audit found that per-record degradations (scope='record' in
# matchday_intelligence.json's degradation_warnings[]) were silently
# filtered out by get_matchday_freshness_warnings — only freshness/subsystem
# scoped warnings propagated. A sustained stream of per-record failures
# (e.g. provider schema drift breaking N injury parses) stayed embedded in
# matchday_intelligence.json with no signal to the dashboard. The fix
# emits a single rollup warning `matchday_record_degradation` so the
# operator sees the data-quality drop without spamming live_state.json
# with one entry per record.
def test_r5_c4_per_record_degradation_rollup_emitted(isolated_paths) -> None:
    """A non-empty stream of per-record degradations (scope='record') in
    matchday_intelligence.json must surface as a single
    `matchday_record_degradation` rollup warning, with a count and a
    per-subsystem breakdown."""
    mod, dash_dir, live_dir = isolated_paths
    out_path = dash_dir / "matchday_intelligence.json"
    embedded = [
        {
            "subsystem": "injury",
            "scope": "record",
            "record_id": "player=Pedri",
            "exception_class": "KeyError",
            "message": "injury record missing 'team'",
            "ts": "2026-06-17T12:00:00+00:00",
        },
        {
            "subsystem": "injury",
            "scope": "record",
            "record_id": "player=Lamine",
            "exception_class": "ValueError",
            "message": "NaN xG",
            "ts": "2026-06-17T12:00:00+00:00",
        },
        {
            "subsystem": "referee",
            "scope": "record",
            "record_id": "ref=Webb",
            "exception_class": "TypeError",
            "message": "expected float, got str",
            "ts": "2026-06-17T12:00:00+00:00",
        },
    ]
    out_path.write_text(json.dumps(_make_consolidated_blob(embedded)))
    results_path = live_dir / "results_2026.json"
    results_path.write_text(json.dumps({"completed_matches": []}))
    _set_relative_age(out_path, results_path, hours_older=1.0)  # fresh
    warnings = mod.get_matchday_freshness_warnings()
    types = [w["type"] for w in warnings]
    assert "matchday_record_degradation" in types, (
        f"per-record degradations did NOT surface as rollup — types={types}"
    )
    # Rollup should NOT also fire matchday_subsystem_stale (per-record !=
    # subsystem-wide collapse).
    assert "matchday_subsystem_stale" not in types, (
        "per-record degradations must NOT trigger matchday_subsystem_stale "
        "(that's reserved for subsystem-wide collapse / freshness)"
    )
    rd = next(w for w in warnings if w["type"] == "matchday_record_degradation")
    assert rd["count"] == 3, f"expected count=3, got {rd.get('count')!r}"
    assert rd["by_subsystem"] == {"injury": 2, "referee": 1}, (
        f"per-subsystem breakdown wrong: {rd.get('by_subsystem')!r}"
    )


def test_r5_c4_zero_record_degradations_no_rollup(isolated_paths) -> None:
    """Negative case: a fresh tick with no per-record degradations must
    NOT spuriously emit the rollup warning."""
    mod, dash_dir, live_dir = isolated_paths
    out_path = dash_dir / "matchday_intelligence.json"
    out_path.write_text(json.dumps(_make_consolidated_blob()))
    results_path = live_dir / "results_2026.json"
    results_path.write_text(json.dumps({"completed_matches": []}))
    _set_relative_age(out_path, results_path, hours_older=0.5)
    warnings = mod.get_matchday_freshness_warnings()
    types = [w["type"] for w in warnings]
    assert "matchday_record_degradation" not in types


# ────────────────────────────────────── R5 C1: provider_returned_nothing warning
# Audit found that fetch_results.py's preservation branch (provider returned
# zero matches AND no warnings, existing file present) would print to stdout
# but emit NO structured warning. The orchestrator's get_results_warnings()
# returned [] and live_state.json carried no signal. Most provider error
# paths DO produce warnings, but a silent-empty case (e.g., auth token
# expired returning HTTP 200 with empty body) escaped the freshness guard
# entirely because the file mtime wasn't updated either. Fix: when the
# preservation branch fires, write a structured `provider_returned_nothing`
# warning into the preserved file's warnings array AND update the mtime so
# get_results_warnings() surfaces it.
def test_r5_c1_provider_returned_nothing_warning_pinned_in_source() -> None:
    """Static pin: the preservation branch in fetch_results.py must emit
    a `provider_returned_nothing` warning so the orchestrator's
    get_results_warnings() can surface it to live_state.json. A revert
    to print-only would mask silent provider failures (200/empty-body
    cases like expired auth tokens that don't raise HTTPError)."""
    src = (ROOT / "scripts" / "live" / "fetch_results.py").read_text()
    # The preservation branch is gated on `if not valid and not warnings_list`.
    branch_idx = src.find("if not valid and not warnings_list and out_path.exists():")
    assert branch_idx > 0, "preservation branch missing in fetch_results.py"
    # Inspect the branch body up to the next blank-line-separated block.
    branch_end = src.find("\n    # ", branch_idx + 1)
    assert branch_end > branch_idx, "could not locate branch end"
    branch_body = src[branch_idx:branch_end]
    assert '"provider_returned_nothing"' in branch_body, (
        "preservation branch missing provider_returned_nothing warning — "
        "a silent-empty provider response (HTTP 200 + empty body, no "
        "exception) will preserve the file without ANY signal reaching "
        "live_state.json. R5 audit C1."
    )
    # And it must actually WRITE the updated file (atomic_write_json),
    # otherwise the warning lands nowhere and mtime stays stale.
    assert "atomic_write_json(out_path, existing)" in branch_body, (
        "preservation branch must atomic_write_json the preserved file "
        "with the new warning + updated_at; otherwise the warning never "
        "lands on disk and freshness guards never re-probe."
    )


# ────────────────────────────────────── R6 M3: warning dedup on preserve
# Audit found that R5 C1's append-on-every-tick pattern grew warnings[]
# unboundedly during sustained provider silent-fails (a 3h outage = 18
# fast ticks = 18 duplicate `provider_returned_nothing` entries).
# Fix: bump count + last_seen_utc on an EXISTING entry; only append a
# fresh entry on the FIRST silent-empty tick.
def test_r6_m3_provider_returned_nothing_dedup_pinned_in_source() -> None:
    """Static pin: the preservation branch must use a dedup-by-type pattern
    (look for existing 'provider_returned_nothing' entry, bump count if
    present, append once otherwise). A revert to unconditional append
    re-introduces the unbounded-growth defect."""
    src = (ROOT / "scripts" / "live" / "fetch_results.py").read_text()
    branch_idx = src.find("if not valid and not warnings_list and out_path.exists():")
    assert branch_idx > 0
    branch_end = src.find("\n    # ", branch_idx + 1)
    body = src[branch_idx:branch_end]
    # The dedup pattern must explicitly look up an existing warning by type
    # before appending. The `next(... if w.get("type") == ...)` form is the
    # canonical idiom; any equivalent (e.g. for-loop with break) would also
    # work but we pin THIS pattern to keep the audit trail tight.
    assert 'w.get("type") == "provider_returned_nothing"' in body, (
        "preservation branch missing dedup check — R6 audit M3. Each silent-"
        "empty tick over a 3h outage previously appended a duplicate entry; "
        "fix is a lookup-and-bump pattern."
    )
    # And the bump path must increment a count + refresh last_seen_utc.
    assert "count" in body and "last_seen_utc" in body, (
        "dedup bump must update both count and last_seen_utc so the "
        "single warning entry carries duration + occurrence information."
    )


# R7 N2: end-to-end functional pin of the R6 M3 dedup path. The static
# pin above proves the LITERAL strings exist in the source; this test
# actually drives main() twice with a silent-empty mock and asserts the
# warnings[] never grows past 1 entry, count climbs, first_seen_utc is
# preserved across ticks, and completed_matches remain locked.
def test_r6_m3_dedup_two_ticks_bumps_count_not_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fetch_results  # type: ignore[import-not-found]

    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True)
    out_path = live_dir / "results_2026.json"
    # Pre-seed with a locked match so the preservation branch fires.
    seeded = {
        "updated_at": "2026-06-17T12:00:00+00:00",
        "source": "api_football",
        "completed_matches": [
            {"m": 1, "home": "MEX", "away": "ESP", "home_score": 1,
             "away_score": 2, "status": "FT"},
        ],
    }
    out_path.write_text(json.dumps(seeded))

    monkeypatch.setattr(fetch_results, "LIVE", live_dir)
    # Force fetch_mock to return [] so (not valid and not warnings_list) is True.
    monkeypatch.setattr(fetch_results, "fetch_mock", lambda: [])
    monkeypatch.setattr(sys, "argv", ["fetch_results.py", "--provider", "mock"])

    # First tick: should append fresh warning with count=1.
    rc1 = fetch_results.main()
    assert rc1 == 0
    after_first = json.loads(out_path.read_text())
    warns1 = [w for w in after_first.get("warnings", [])
              if isinstance(w, dict) and w.get("type") == "provider_returned_nothing"]
    assert len(warns1) == 1, f"first tick: expected 1 warning, got {warns1}"
    w1 = warns1[0]
    assert w1["count"] == 1
    assert "first_seen_utc" in w1 and w1["first_seen_utc"]
    assert "last_seen_utc" in w1 and w1["last_seen_utc"]
    first_seen_after_t1 = w1["first_seen_utc"]
    # Locked match preserved.
    assert len(after_first["completed_matches"]) == 1
    assert after_first["completed_matches"][0]["m"] == 1

    # Second tick: bump count, preserve first_seen_utc, refresh last_seen_utc.
    rc2 = fetch_results.main()
    assert rc2 == 0
    after_second = json.loads(out_path.read_text())
    warns2 = [w for w in after_second.get("warnings", [])
              if isinstance(w, dict) and w.get("type") == "provider_returned_nothing"]
    assert len(warns2) == 1, (
        f"second tick must NOT append a duplicate — got {len(warns2)} entries: {warns2}. "
        "This is the unbounded-growth regression R6 M3 fixed."
    )
    w2 = warns2[0]
    assert w2["count"] == 2, f"count must bump from 1 → 2, got {w2['count']}"
    assert w2["first_seen_utc"] == first_seen_after_t1, (
        "first_seen_utc must be preserved across ticks so the dashboard "
        "can show outage onset, not most-recent-bump time."
    )
    # last_seen_utc must be ≥ previous (monotonic non-decreasing).
    assert w2["last_seen_utc"] >= w1["last_seen_utc"]
    # Completed matches still locked.
    assert len(after_second["completed_matches"]) == 1


# R7 N3: a pre-R6 warning entry written before the dedup fields existed
# must still get first_seen_utc backfilled on the next bump, so the
# dashboard never shows a missing-field surprise after a deploy that
# catches an outage already in progress.
def test_r7_n3_first_seen_utc_backfilled_on_legacy_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fetch_results  # type: ignore[import-not-found]

    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True)
    out_path = live_dir / "results_2026.json"
    # Pre-seed with a legacy pre-R6 warning: no first_seen_utc / last_seen_utc / count.
    seeded = {
        "updated_at": "2026-06-17T12:00:00+00:00",
        "source": "api_football",
        "completed_matches": [
            {"m": 1, "home": "MEX", "away": "ESP", "home_score": 1,
             "away_score": 2, "status": "FT"},
        ],
        "warnings": [
            {"type": "provider_returned_nothing",
             "message": "old pre-R6 warning shape — no count, no first_seen_utc"},
        ],
    }
    out_path.write_text(json.dumps(seeded))

    monkeypatch.setattr(fetch_results, "LIVE", live_dir)
    monkeypatch.setattr(fetch_results, "fetch_mock", lambda: [])
    monkeypatch.setattr(sys, "argv", ["fetch_results.py", "--provider", "mock"])

    rc = fetch_results.main()
    assert rc == 0
    bumped = json.loads(out_path.read_text())
    warns = [w for w in bumped.get("warnings", [])
             if isinstance(w, dict) and w.get("type") == "provider_returned_nothing"]
    assert len(warns) == 1
    w = warns[0]
    # count starts from 1 (legacy entry assumed count=1) → bumps to 2.
    assert w["count"] == 2
    # last_seen_utc must be set (didn't exist on legacy entry).
    assert "last_seen_utc" in w and w["last_seen_utc"]
    # first_seen_utc must be backfilled even though it was missing on the
    # legacy entry — without the R7 N3 setdefault the dashboard would
    # show an undefined field after the first post-deploy bump.
    assert "first_seen_utc" in w and w["first_seen_utc"], (
        "first_seen_utc must be backfilled on legacy entries — R7 N3."
    )


# ────────────────────────────────────── R6 M2: provider fallback warning
# Audit found that when the operator requests a real provider (e.g.
# FOOTBALL_PROVIDER=api_football) but the corresponding key is unset or
# empty, detect_provider_source() silently returns ("manual/mock",
# "manual"). The dashboard sees `source="manual/mock"` but no warning
# explains WHY the fallback fired — operators miss rotated/expired
# secrets. Fix: a new _provider_fallback_warnings() helper emits a
# structured `provider_key_missing` warning that flows into every
# write_live_state path alongside mf_warnings.
def test_r6_m2_provider_fallback_helper_exists() -> None:
    """The `_provider_fallback_warnings()` helper must exist in
    run_live_update.py and emit a `provider_key_missing` warning when
    the requested provider's key is missing."""
    src = _read_source()
    assert "def _provider_fallback_warnings" in src, (
        "_provider_fallback_warnings helper missing — R6 audit M2. "
        "Silent provider-key fallback re-introduces the dashboard-blind "
        "failure mode where operators miss rotated/expired secrets."
    )
    assert '"provider_key_missing"' in src, (
        "_provider_fallback_warnings must emit the canonical "
        "`provider_key_missing` warning type so the dashboard can "
        "surface it as a distinct pill."
    )


def test_r6_m2_provider_fallback_merged_into_mf_warnings() -> None:
    """The fallback helper must be folded into the mf_warnings probe at
    the top of main() so EVERY write_live_state path (including the
    three early-exit guards) carries the signal. Pinning the merge
    keeps a refactor from silently dropping the fold."""
    src = _read_source()
    # Locate the mf_warnings assignment block.
    mf_idx = src.find("mf_warnings = (")
    if mf_idx < 0:
        # tolerate the inline form too
        mf_idx = src.find("mf_warnings =")
    assert mf_idx > 0, "mf_warnings assignment not found"
    # Take a window large enough to span a multi-line assignment.
    window = src[mf_idx:mf_idx + 500]
    assert "_matchday_freshness_warnings_safe()" in window
    assert "_provider_fallback_warnings()" in window, (
        "mf_warnings assignment does not include _provider_fallback_warnings() "
        "— provider-key fallback warnings won't reach live_state.json on "
        "any tick. R6 audit M2."
    )


@pytest.fixture
def env_isolated(monkeypatch: pytest.MonkeyPatch):
    """Isolate provider/key env vars so each test owns its own state."""
    for var in (
        "FOOTBALL_PROVIDER", "WC_RESULTS_SOURCE",
        "API_FOOTBALL_KEY", "WC_APIFOOTBALL_KEY",
        "FOOTBALL_DATA_TOKEN", "WC_FOOTBALL_DATA_TOKEN",
        "SPORTMONKS_TOKEN", "WC_SPORTMONKS_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_r6_m2_fallback_warning_emitted_when_api_football_key_missing(env_isolated) -> None:
    """Functional: FOOTBALL_PROVIDER=api_football + no API_FOOTBALL_KEY
    → _provider_fallback_warnings() returns a non-empty list with the
    correct type + missing_env_var fields."""
    env_isolated.setenv("FOOTBALL_PROVIDER", "api_football")
    import run_live_update as rlu
    warnings = rlu._provider_fallback_warnings()
    assert len(warnings) == 1
    w = warnings[0]
    assert w["type"] == "provider_key_missing"
    assert w["requested_provider"] == "api_football"
    assert w["missing_env_var"] == "API_FOOTBALL_KEY"


def test_r6_m2_no_warning_when_provider_mock(env_isolated) -> None:
    """Negative: FOOTBALL_PROVIDER unset (defaults to mock) → no warning.
    Pins the contract that mock mode is OPTED INTO, not a regression."""
    import run_live_update as rlu
    assert rlu._provider_fallback_warnings() == []


def test_r6_m2_no_warning_when_key_present(env_isolated) -> None:
    """Negative: FOOTBALL_PROVIDER=api_football + API_FOOTBALL_KEY set →
    no warning. Pins the happy path."""
    env_isolated.setenv("FOOTBALL_PROVIDER", "api_football")
    env_isolated.setenv("API_FOOTBALL_KEY", "sk-test-value")
    import run_live_update as rlu
    assert rlu._provider_fallback_warnings() == []


def test_r6_m2_legacy_alias_satisfies_key_check(env_isolated) -> None:
    """The legacy WC_APIFOOTBALL_KEY alias must also satisfy the key
    check — operators with the old env var name shouldn't see a spurious
    `provider_key_missing` warning."""
    env_isolated.setenv("FOOTBALL_PROVIDER", "api_football")
    env_isolated.setenv("WC_APIFOOTBALL_KEY", "sk-legacy-alias-value")
    import run_live_update as rlu
    assert rlu._provider_fallback_warnings() == []


def test_r6_m2_sportmonks_provider_missing_key(env_isolated) -> None:
    """The fallback check must cover all three real providers — not just
    api_football."""
    env_isolated.setenv("FOOTBALL_PROVIDER", "sportmonks")
    import run_live_update as rlu
    warnings = rlu._provider_fallback_warnings()
    assert len(warnings) == 1
    assert warnings[0]["missing_env_var"] == "SPORTMONKS_TOKEN"


def test_r6_m2_crash_handler_carries_provider_fallback_warning() -> None:
    """An orchestrator_crash + missing-provider-key combo must surface
    BOTH signals on live_state.json: the crash entry hints WHAT failed,
    the provider-fallback entry hints WHY (stale data heading into the
    crash). Pre-fix the crash handler only re-probed freshness, omitting
    the provider-fallback signal — operators investigating a crash would
    miss the rotated secret context."""
    src = _read_source()
    # Locate the orchestrator-crash block.
    crash_idx = src.find("[run_live_update] FATAL")
    assert crash_idx > 0, "orchestrator-crash handler missing"
    crash_block = src[crash_idx:]
    assert "_provider_fallback_warnings()" in crash_block, (
        "crash handler does NOT call _provider_fallback_warnings() — a "
        "crash that occurs while the provider key is missing will surface "
        "the crash but hide the WHY signal. R6 audit M2 follow-up."
    )


# R8 O1: sim subprocess stderr must flow into the operator-visible
# sim_failure warning. Pre-R8 the R7 N1 RuntimeError diagnostic landed
# only in CI logs — operators saw a generic pill with no hint about
# annex_c miss / fallback exhaustion / config file to inspect.
def test_r8_o1_run_capture_helper_exists_and_returns_rc_plus_stderr(
    tmp_path: Path,
) -> None:
    """run_capture() returns (rc, stderr_text). Functional unit test:
    invoke a child Python that writes to stderr and exits non-zero;
    verify both fields come back."""
    import run_live_update as rlu  # type: ignore[import-not-found]
    rc, stderr = rlu.run_capture([
        sys.executable, "-c",
        "import sys; sys.stderr.write('R8 O1 marker\\n'); sys.exit(2)",
    ])
    assert rc == 2
    assert "R8 O1 marker" in stderr


def test_r8_o1_sim_failure_warning_includes_stderr_tail_pinned_in_source() -> None:
    """Static pin: the sim subprocess invocation must use run_capture()
    (not the plain run()), AND the constructed sim_failure warning must
    fold stderr into the message. A revert to run() breaks the operator
    observability path the R7 N1 diagnostic relies on."""
    src = _read_source()
    sim_idx = src.find("scripts/03_simulate.py")
    assert sim_idx > 0, "sim invocation not found"
    # The line immediately around the sim subprocess must be run_capture.
    sim_block = src[max(0, sim_idx - 400):sim_idx + 600]
    assert "run_capture(" in sim_block, (
        "sim subprocess invocation must use run_capture() so stderr "
        "can flow into the sim_failure warning. R8 audit O1."
    )
    # And the sim_failure warning must read stderr into its message.
    sf_idx = src.find('"type": "sim_failure"')
    assert sf_idx > 0
    sf_block = src[max(0, sf_idx - 400):sf_idx + 400]
    assert "stderr_tail" in sf_block or "sim_stderr" in sf_block, (
        "sim_failure warning must fold captured stderr into the message; "
        "otherwise the R7 N1 RuntimeError diagnostic is invisible to "
        "operators (lands only in CI logs). R8 audit O1."
    )
