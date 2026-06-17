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


def _make_consolidated_blob(degradation_warnings=None) -> dict:
    """Minimal matchday_intelligence.json shape — just enough for the
    helper's parser. Real schema has many more keys; the helper only
    touches `degradation_warnings`."""
    return {
        "generated_at": "2026-06-17T12:00:00+00:00",
        "schema_version": 1,
        "active_adjustments": [],
        "summary": {},
        "warnings": [],
        "degradation_warnings": degradation_warnings or [],
    }


def _touch_age(path: Path, hours_old: float) -> None:
    """Set the file's mtime to `hours_old` hours BEFORE now."""
    now = os.path.getmtime(path)
    target = now - (hours_old * 3600.0)
    os.utime(path, (target, target))


def _set_relative_age(file_path: Path, ref_path: Path, hours_older: float) -> None:
    """Set `file_path` mtime to be `hours_older` hours OLDER than `ref_path`'s
    current mtime. Both files must already exist."""
    ref_mtime = os.path.getmtime(ref_path)
    target = ref_mtime - (hours_older * 3600.0)
    os.utime(file_path, (target, target))


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
    # Probe call appears before the circuit-breaker guard.
    cb_idx = src.find("Circuit breaker tripped")
    probe_idx = src.find("mf_warnings = _matchday_freshness_warnings_safe()")
    assert probe_idx > 0, "freshness probe call missing"
    assert probe_idx < cb_idx, (
        "freshness probe must precede the circuit-breaker guard so "
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
