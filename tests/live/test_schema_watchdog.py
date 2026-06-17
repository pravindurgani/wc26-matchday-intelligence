"""Tests for scripts/live/_schema_watchdog.py.

Coverage:
- compute_shape_hash is stable (same input -> same hash)
- shape-only: differing values with same shape -> same hash
- drift detection: key add / key remove / type change
- nested key rename detected (the `goals.{conceded}` -> `goals.{against}` case)
- list element shape is captured (not just "list")
- empty list distinguishes from list of {}
- bool is NOT confused with int (despite Python subclassing)
- CLI exit codes: 0 on match, 8 on drift, 2 on usage error
- load_baseline loads the captured baseline file format correctly
- assert_shape soft mode: warns but does not raise
- assert_shape strict mode: raises SchemaDriftError
- assert_shape missing baseline: warns, returns True (does not break tick)
- snapshot CLI writes a valid baseline that round-trips
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.live._schema_watchdog import (  # noqa: E402
    EXIT_DRIFT,
    EXIT_OK,
    EXIT_USAGE,
    SchemaDriftError,
    assert_shape,
    compute_shape_hash,
    diff_shapes,
    extract_shape,
    load_baseline,
    main,
    shape_tree,
    write_baseline,
)

WATCHDOG = REPO / "scripts" / "live" / "_schema_watchdog.py"
SAMPLES = REPO / "tests" / "live" / "provider_samples"
BASELINES = REPO / "data" / "live" / "_provider_schemas"


# ---------------------------------------------------------------------------
# 1. compute_shape_hash: stability + shape-only
# ---------------------------------------------------------------------------

def test_hash_is_stable_across_calls():
    obj = {"a": 1, "b": [{"id": 9, "name": "x"}]}
    assert compute_shape_hash(obj) == compute_shape_hash(obj)


def test_hash_is_value_independent():
    """Different values, same shape -> same hash."""
    a = {"id": 1, "name": "Mexico", "score": 2}
    b = {"id": 9999, "name": "Norway", "score": 0}
    assert compute_shape_hash(a) == compute_shape_hash(b)


def test_hash_independent_of_dict_iteration_order():
    a = {"x": 1, "y": "s", "z": True}
    b = {"z": True, "y": "s", "x": 1}
    assert compute_shape_hash(a) == compute_shape_hash(b)


# ---------------------------------------------------------------------------
# 2. Drift detection at the top level
# ---------------------------------------------------------------------------

def test_hash_drifts_on_key_addition():
    a = {"id": 1, "name": "x"}
    b = {"id": 1, "name": "x", "new_field": 0}
    assert compute_shape_hash(a) != compute_shape_hash(b)


def test_hash_drifts_on_key_removal():
    a = {"id": 1, "name": "x"}
    b = {"id": 1}
    assert compute_shape_hash(a) != compute_shape_hash(b)


def test_hash_drifts_on_type_change():
    a = {"id": 1}        # int
    b = {"id": "1"}      # str
    assert compute_shape_hash(a) != compute_shape_hash(b)


def test_hash_drifts_on_int_to_float():
    a = {"score": 1}
    b = {"score": 1.0}
    assert compute_shape_hash(a) != compute_shape_hash(b)


def test_bool_is_distinct_from_int():
    """Python's bool is a subclass of int — make sure we don't confuse them."""
    a = {"flag": True}
    b = {"flag": 1}
    assert compute_shape_hash(a) != compute_shape_hash(b)


def test_null_vs_present_value_differs():
    a = {"goals": None}
    b = {"goals": 0}
    assert compute_shape_hash(a) != compute_shape_hash(b)


# ---------------------------------------------------------------------------
# 3. Nested drift — the clean_sheets-style rename
# ---------------------------------------------------------------------------

def test_hash_drifts_on_nested_key_rename():
    """The canonical case: `goals: {total, conceded}` -> `goals: {total, against}`."""
    before = {"player": {"id": 1, "name": "x"},
              "goals":  {"total": 5, "conceded": 0}}
    after  = {"player": {"id": 1, "name": "x"},
              "goals":  {"total": 5, "against":  0}}
    assert compute_shape_hash(before) != compute_shape_hash(after)


def test_hash_drifts_on_nested_added_key():
    before = {"goals": {"total": 5}}
    after  = {"goals": {"total": 5, "clean_sheets": 0}}
    assert compute_shape_hash(before) != compute_shape_hash(after)


def test_hash_drifts_on_dropped_nested_key():
    before = {"goals": {"total": 5, "clean_sheets": 0}}
    after  = {"goals": {"total": 5}}
    assert compute_shape_hash(before) != compute_shape_hash(after)


# ---------------------------------------------------------------------------
# 4. List shape captured
# ---------------------------------------------------------------------------

def test_list_element_shape_captured():
    a = {"items": [{"id": 1, "name": "x"}]}
    b = {"items": [{"id": 1}]}        # element lost a key
    assert compute_shape_hash(a) != compute_shape_hash(b)


def test_homogeneous_lists_use_first_element():
    """Different N items, same per-item shape -> same hash."""
    a = {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}
    b = {"items": [{"id": 99}]}
    assert compute_shape_hash(a) == compute_shape_hash(b)


def test_empty_list_distinct_from_populated_list():
    a = {"items": []}
    b = {"items": [{"id": 1}]}
    assert compute_shape_hash(a) != compute_shape_hash(b)


# ---------------------------------------------------------------------------
# 5. diff_shapes — human readable
# ---------------------------------------------------------------------------

def test_diff_reports_added_removed_changed():
    base = {"id": 1, "name": "x", "score": 0}
    cur  = {"id": "1", "name": "x", "extra": True}  # type change + add + remove
    diff = diff_shapes(cur, base)
    joined = "\n".join(diff)
    assert "ADDED" in joined and "extra" in joined
    assert "REMOVED" in joined and "score" in joined
    assert "CHANGED" in joined and "id" in joined


def test_identical_shape_yields_empty_diff():
    a = {"id": 1, "name": "x"}
    b = {"id": 9, "name": "y"}
    assert diff_shapes(a, b) == []


# ---------------------------------------------------------------------------
# 6. Baseline I/O
# ---------------------------------------------------------------------------

def test_write_baseline_then_load_round_trips(tmp_path: Path):
    obj = {"response": [{"id": 1, "name": "x"}], "results": 1}
    bp = tmp_path / "x.shape.json"
    written = write_baseline(bp, obj, captured="2026-06-17")
    assert bp.exists()
    loaded = load_baseline(bp)
    assert loaded["hash"] == written["hash"] == compute_shape_hash(obj)
    assert loaded["captured"] == "2026-06-17"
    # Shape tree should be a dict mirroring our object.
    assert "response" in loaded["shape_tree"]


def test_load_baseline_rejects_missing_keys(tmp_path: Path):
    bad = tmp_path / "bad.shape.json"
    bad.write_text(json.dumps({"foo": "bar"}))
    with pytest.raises(ValueError, match="missing required keys"):
        load_baseline(bad)


def test_load_existing_captured_baseline_format():
    """The format we just captured for the 4 provider samples must load."""
    bp = BASELINES / "apifootball_fixtures_events.shape.json"
    assert bp.exists(), "baseline must be captured by the watchdog setup"
    data = load_baseline(bp)
    assert isinstance(data["hash"], str) and len(data["hash"]) == 16
    assert "captured" in data
    assert isinstance(data["shape_tree"], dict)


def test_all_captured_baselines_still_match_their_samples():
    """Sanity: every baseline file matches a freshly-rehashed sample."""
    cases = [
        ("apifootball_events_sample.json",
         "apifootball_fixtures_events.shape.json"),
        ("api_football_fixture_response.json",
         "apifootball_fixtures.shape.json"),
        ("apifootball_euro2024_knockouts.json",
         "apifootball_euro2024_knockouts.shape.json"),
        ("apifootball_wc2022_knockouts.json",
         "apifootball_wc2022_knockouts.shape.json"),
    ]
    for sample, baseline in cases:
        obj = json.loads((SAMPLES / sample).read_text())
        bp  = load_baseline(BASELINES / baseline)
        assert compute_shape_hash(obj) == bp["hash"], (
            f"sample {sample} drifted from baseline {baseline}"
        )


# ---------------------------------------------------------------------------
# 7. assert_shape — soft vs strict
# ---------------------------------------------------------------------------

def test_assert_shape_returns_true_on_match(tmp_path: Path, caplog):
    obj = {"id": 1, "name": "x"}
    bp = tmp_path / "b.shape.json"
    write_baseline(bp, obj)
    with caplog.at_level(logging.WARNING, logger="schema_watchdog"):
        assert assert_shape(obj, bp) is True
    assert "DRIFT" not in caplog.text


def test_assert_shape_soft_mode_warns_on_drift(tmp_path: Path, caplog):
    bp = tmp_path / "b.shape.json"
    write_baseline(bp, {"id": 1})
    drifted = {"id": 1, "added": True}
    with caplog.at_level(logging.WARNING, logger="schema_watchdog"):
        result = assert_shape(drifted, bp)
    assert result is False
    assert "DRIFT" in caplog.text
    assert "ADDED" in caplog.text


def test_assert_shape_strict_mode_raises_on_drift(tmp_path: Path):
    bp = tmp_path / "b.shape.json"
    write_baseline(bp, {"id": 1})
    with pytest.raises(SchemaDriftError, match="DRIFT"):
        assert_shape({"id": 1, "added": True}, bp, raise_on_drift=True)


def test_assert_shape_missing_baseline_does_not_break(tmp_path: Path, caplog):
    bp = tmp_path / "nope.shape.json"
    with caplog.at_level(logging.WARNING, logger="schema_watchdog"):
        result = assert_shape({"id": 1}, bp)
    assert result is True
    assert "baseline missing" in caplog.text


# ---------------------------------------------------------------------------
# 8. CLI exit codes
# ---------------------------------------------------------------------------

def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(WATCHDOG), *argv],
        capture_output=True, text=True, cwd=str(REPO),
    )


def test_cli_check_exit_zero_on_match(tmp_path: Path):
    obj = {"id": 1, "name": "x"}
    resp = tmp_path / "r.json"; resp.write_text(json.dumps(obj))
    base = tmp_path / "b.shape.json"; write_baseline(base, obj)
    r = _run_cli("check", str(resp), str(base))
    assert r.returncode == EXIT_OK == 0
    assert "OK" in r.stdout


def test_cli_check_exit_eight_on_drift(tmp_path: Path):
    base_obj = {"id": 1}
    drifted = {"id": "1", "extra": True}
    resp = tmp_path / "r.json"; resp.write_text(json.dumps(drifted))
    base = tmp_path / "b.shape.json"; write_baseline(base, base_obj)
    r = _run_cli("check", str(resp), str(base))
    assert r.returncode == EXIT_DRIFT == 8
    assert "DRIFT" in r.stdout
    # The diff should be human-readable.
    assert "ADDED" in r.stdout or "CHANGED" in r.stdout


def test_cli_check_exit_two_when_response_missing(tmp_path: Path):
    base = tmp_path / "b.shape.json"; write_baseline(base, {"id": 1})
    r = _run_cli("check", str(tmp_path / "nope.json"), str(base))
    assert r.returncode == EXIT_USAGE == 2


def test_cli_check_exit_two_when_baseline_missing(tmp_path: Path):
    resp = tmp_path / "r.json"; resp.write_text("{}")
    r = _run_cli("check", str(resp), str(tmp_path / "nope.shape.json"))
    assert r.returncode == EXIT_USAGE


def test_cli_snapshot_writes_loadable_baseline(tmp_path: Path):
    obj = {"id": 1, "items": [{"x": "y"}]}
    resp = tmp_path / "r.json"; resp.write_text(json.dumps(obj))
    base = tmp_path / "b.shape.json"
    r = _run_cli("snapshot", str(resp), str(base), "--captured", "2026-06-17")
    assert r.returncode == EXIT_OK
    assert base.exists()
    loaded = load_baseline(base)
    assert loaded["hash"] == compute_shape_hash(obj)
    assert loaded["captured"] == "2026-06-17"


# ---------------------------------------------------------------------------
# 9. main() entry-point also returns the right codes (in-process)
# ---------------------------------------------------------------------------

def test_main_returns_eight_on_drift(tmp_path: Path, capsys):
    base = tmp_path / "b.shape.json"; write_baseline(base, {"id": 1})
    drifted = tmp_path / "r.json"; drifted.write_text(json.dumps({"id": 1, "x": 0}))
    code = main(["check", str(drifted), str(base)])
    assert code == EXIT_DRIFT
    out = capsys.readouterr().out
    assert "DRIFT" in out


def test_main_returns_zero_on_match(tmp_path: Path, capsys):
    base = tmp_path / "b.shape.json"; write_baseline(base, {"id": 1})
    same = tmp_path / "r.json"; same.write_text(json.dumps({"id": 999}))
    code = main(["check", str(same), str(base)])
    assert code == EXIT_OK


# ---------------------------------------------------------------------------
# 10. extract_shape / shape_tree are exercised together
# ---------------------------------------------------------------------------

def test_shape_tree_is_human_readable_dict():
    tree = shape_tree({"id": 1, "name": "x", "kids": [{"id": 2}]})
    assert tree == {"id": "int", "name": "str", "kids": [{"id": "int"}]}


def test_extract_shape_uses_sorted_dict_items():
    s = extract_shape({"b": 1, "a": "s"})
    assert s[0] == "dict"
    assert s[1][0][0] == "a"
    assert s[1][1][0] == "b"
