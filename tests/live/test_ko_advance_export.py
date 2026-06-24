"""
S7 — KO advance-prob export tests.

Covers the four invariants for scripts/live/export_ko_advance.py:

  1. End-to-end on a synthetic feed with 1 resolved KO match + 31
     placeholders → exactly 1 entry in match_predictions_ko with
     p_advance_match ≈ p_home_win + 0.5 * p_draw.

  2. Idempotency: running the post-processor twice on the same input
     produces byte-identical output (no duplication, no compounding).

  3. Σ-gate still passes after export (the new field is per-match, not
     part of any sum-to-1 channel).

  4. Placeholder-only bracket (no completed_matches) → match_predictions_ko
     is empty AND Σ-gate still passes. This is today's state (group stage
     in progress, zero KO matches resolved).
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
LIVE = SCRIPTS / "live"

# Mirror sys.path the same way the production module does so the imports
# below resolve regardless of pytest's discovery order.
for p in (str(SCRIPTS), str(LIVE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from check_invariants import check_invariants  # noqa: E402

from scripts.live.export_ko_advance import (  # noqa: E402
    _build_nb_dc_matrix,
    _wdl_from_matrix,
    build_ko_advance_entries,
    export,
)


# --------------------------------------------------------------------------
# Tiny synthetic feed builders. Keep the surface minimal — only the fields
# check_invariants and the export touch are populated; everything else is
# stubbed at the smallest sane value so the gate stays green.
# --------------------------------------------------------------------------
def _minimal_team_predictions() -> list[dict]:
    """48 teams with p_champion = 1/48 — passes check_invariants exactly."""
    return [{"team": f"T{i:02d}", "p_champion": 1.0 / 48} for i in range(48)]


def _real_team_predictions() -> list[dict]:
    """48 real team names so we can wire knock_lambdas_table entries that
    reference 'Argentina', 'Brazil', etc. — same Σ p_champion = 1/48."""
    teams = [
        "Argentina", "Brazil", "France", "Spain", "England", "Portugal",
        "Germany", "Netherlands", "Italy", "Belgium", "Croatia", "Uruguay",
        "Colombia", "Switzerland", "Mexico", "United States",
        "Canada", "Morocco", "Senegal", "Japan", "South Korea", "Iran",
        "Australia", "Saudi Arabia", "Qatar", "Egypt", "Tunisia", "Algeria",
        "Norway", "Sweden", "Austria", "Czechia", "Scotland", "Turkey",
        "Bosnia and Herzegovina", "Ivory Coast", "DR Congo", "Cape Verde",
        "Ghana", "South Africa", "Iraq", "Jordan", "Uzbekistan",
        "New Zealand", "Panama", "Haiti", "Curacao", "Ecuador",
    ]
    assert len(teams) == 48
    return [{"team": t, "p_champion": 1.0 / 48} for t in teams]


def _write_min_results(tmp: Path) -> Path:
    """Empty completed_matches — same shape as today's results_2026.json."""
    p = tmp / "results_2026.json"
    p.write_text(json.dumps({
        "schema": "test",
        "updated_at": "2026-06-17T00:00:00Z",
        "source": "test",
        "completed_matches": [],
        "in_play": [],
        "warnings": [],
    }))
    return p


def _write_full_results_through_r32(tmp: Path) -> Path:
    """Pretend the entire group stage AND R32 played out: 72 group matches
    locked + 16 R32 matches locked. The exact teams / scores do not matter
    for the test — what matters is that the *number* of completed matches
    plus the winner field per record is enough to resolve every R16 slot
    via the `W<n>` walk in _resolve_slot.

    We fabricate a deterministic schedule that resolves to itself: each R32
    match's `winner` is `home`, so R16 slot 'W74' resolves to the home of
    M74, and the (home, away) pair for the R16 match is then deterministic
    and we can pin a knock_lambdas_table entry to it.
    """
    # The synthetic R32 fixtures (matching the official bracket m=73..88).
    # Names are dummies; only consistency matters.
    r32_records = []
    for i, m_num in enumerate(range(73, 89)):
        r32_records.append({
            "m": m_num,
            "home": f"R32_H{m_num}",
            "away": f"R32_A{m_num}",
            "home_score": 1,
            "away_score": 0,
            "home_pens": None,
            "away_pens": None,
            "winner": "home",
            "status": "FT",
            "source": "test",
            "updated_at": "2026-06-30T00:00:00Z",
        })
    p = tmp / "results_2026.json"
    p.write_text(json.dumps({
        "schema": "test",
        "updated_at": "2026-06-30T00:00:00Z",
        "source": "test",
        "completed_matches": r32_records,
        "in_play": [],
        "warnings": [],
    }))
    return p


def _minimal_bracket() -> dict:
    """Full bracket shape but slot codes only — never resolves to teams
    unless results are populated for `W<n>` walks AND/OR a group-slot
    resolver is in play. We use it to test 'placeholder-only' behavior."""
    return {
        "r32_slots": [
            {"match_num": m, "slot_a": "1A", "slot_b": "2B",
             "date": "2026-06-28", "venue": "X", "next_match": 89}
            for m in range(73, 89)
        ],
        "r16_bracket": [
            {"match_num": m,
             "slot_a": f"W{m - 16}", "slot_b": f"W{m - 15}",
             "date": "2026-07-04", "venue": "X"}
            for m in range(89, 97)
        ],
        "qf_bracket": [
            {"match_num": m, "slot_a": f"W{m - 8}", "slot_b": f"W{m - 7}",
             "date": "2026-07-09", "venue": "X"}
            for m in range(97, 101)
        ],
        "sf_bracket": [
            {"match_num": m, "slot_a": f"W{m - 4}", "slot_b": f"W{m - 3}",
             "date": "2026-07-14", "venue": "X"}
            for m in range(101, 103)
        ],
        "final_and_third_place": {
            "third_place": {"match_num": 103, "slot_a": "L101", "slot_b": "L102",
                            "date": "2026-07-18", "venue": "X"},
            "final": {"match_num": 104, "slot_a": "W101", "slot_b": "W102",
                      "date": "2026-07-19", "venue": "X"},
        },
        "source": "test",
    }


def _bracket_with_one_resolved_r16(home: str, away: str) -> dict:
    """Same as _minimal_bracket but R16 m=89's slots are concrete team
    names — guaranteed to resolve via the 'else: return as-is' branch in
    _resolve_slot. All other KO matches stay as placeholder slot codes."""
    br = _minimal_bracket()
    br["r16_bracket"][0]["slot_a"] = home
    br["r16_bracket"][0]["slot_b"] = away
    return br


def _knock_lambdas_table_one_pair(home: str, away: str,
                                  lam_h: float = 1.6, lam_a: float = 1.1) -> list[dict]:
    """Single-pair λ table. Real sim emits the full (48*47) directed
    matrix; we only need the one pair under test."""
    return [{
        "home": home, "away": away,
        "lambda_home": lam_h, "lambda_away": lam_a,
        "effective_elo_home": 1700.0, "effective_elo_away": 1500.0,
    }]


def _feed_with_one_resolved_ko(home: str, away: str,
                               lam_h: float = 1.6,
                               lam_a: float = 1.1) -> dict:
    """Synthetic predictions blob: 48 teams Σ=1, full bracket with one
    R16 slot pre-resolved to concrete team names, knock_lambdas_table
    carrying exactly one (home, away) pair."""
    return {
        "team_predictions": _minimal_team_predictions(),
        "match_predictions": [],
        "bracket": _bracket_with_one_resolved_r16(home, away),
        "knock_lambdas_table": _knock_lambdas_table_one_pair(home, away,
                                                             lam_h, lam_a),
    }


# --------------------------------------------------------------------------
# Test (1): end-to-end with exactly one resolved KO match.
# --------------------------------------------------------------------------
def test_single_resolved_ko_writes_one_entry(tmp_path: Path) -> None:
    in_path = tmp_path / "predictions_live.json"
    in_path.write_text(json.dumps(_feed_with_one_resolved_ko("Argentina",
                                                             "Brazil")))
    out_path = tmp_path / "predictions_live.out.json"

    # Bracket + results live in tmp_path too so no real-repo state leaks
    # into the test (each test owns its inputs end-to-end).
    bracket_path = tmp_path / "bracket.json"
    bracket_path.write_text(json.dumps(
        _bracket_with_one_resolved_r16("Argentina", "Brazil")))
    results_path = _write_min_results(tmp_path)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"groups": {}, "group_stage_schedule": [],
                                    "fifa_rankings_june_2026": {}}))
    annex_path = tmp_path / "annex.json"
    annex_path.write_text(json.dumps({"table": {}}))

    payload = export(in_path=in_path, out_path=out_path,
                     bracket_path=bracket_path, results_path=results_path,
                     cfg_path=cfg_path, annex_c_path=annex_path)

    ko = payload.get("match_predictions_ko")
    assert ko is not None, "match_predictions_ko key must be present"
    assert len(ko) == 1, f"expected exactly 1 resolved KO entry, got {len(ko)}"
    e = ko[0]
    assert e["m"] == 89
    assert e["home"] == "Argentina"
    assert e["away"] == "Brazil"
    assert e["stage"] == "r16"

    # p_advance_match must equal p_home_win + 0.5 * p_draw to floating-point.
    expected_adv = e["p_home_win"] + 0.5 * e["p_draw"]
    assert abs(e["p_advance_match"] - expected_adv) < 1e-12, (
        f"p_advance_match drift: {e['p_advance_match']} vs "
        f"{expected_adv} (Δ={abs(e['p_advance_match'] - expected_adv):.3e})"
    )

    # And the WDL must equal the NB+DC matrix at the same (lam_h, lam_a).
    M = _build_nb_dc_matrix(1.6, 1.1)
    truth_h, truth_d, truth_a = _wdl_from_matrix(M)
    assert abs(e["p_home_win"] - truth_h) < 1e-12
    assert abs(e["p_draw"] - truth_d) < 1e-12
    assert abs(e["p_away_win"] - truth_a) < 1e-12

    # And p_advance_match must agree with the closed-form right-hand side.
    assert abs(e["p_advance_match"] - (truth_h + 0.5 * truth_d)) < 1e-12

    # Range sanity.
    assert 0.0 <= e["p_advance_match"] <= 1.0


# --------------------------------------------------------------------------
# Test (2): idempotency — re-running on already-augmented data is a no-op.
# --------------------------------------------------------------------------
def test_idempotent_double_run(tmp_path: Path) -> None:
    in_path = tmp_path / "predictions_live.json"
    in_path.write_text(json.dumps(_feed_with_one_resolved_ko("Argentina",
                                                             "Brazil")))
    out_path = tmp_path / "predictions_live.out.json"

    bracket_path = tmp_path / "bracket.json"
    bracket_path.write_text(json.dumps(
        _bracket_with_one_resolved_r16("Argentina", "Brazil")))
    results_path = _write_min_results(tmp_path)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"groups": {}, "group_stage_schedule": [],
                                    "fifa_rankings_june_2026": {}}))
    annex_path = tmp_path / "annex.json"
    annex_path.write_text(json.dumps({"table": {}}))

    # Run #1: in -> out
    export(in_path=in_path, out_path=out_path,
           bracket_path=bracket_path, results_path=results_path,
           cfg_path=cfg_path, annex_c_path=annex_path)
    first = out_path.read_text()

    # Run #2: out -> out (already-augmented input, same bracket/results).
    export(in_path=out_path, out_path=out_path,
           bracket_path=bracket_path, results_path=results_path,
           cfg_path=cfg_path, annex_c_path=annex_path)
    second = out_path.read_text()

    assert first == second, (
        "Second export must produce byte-identical output (idempotency)."
    )
    # Belt + braces: the KO block didn't double — still exactly one entry.
    payload = json.loads(second)
    assert len(payload["match_predictions_ko"]) == 1


# --------------------------------------------------------------------------
# Test (3): Σ-gate still passes after a populated export.
# --------------------------------------------------------------------------
def test_sigma_gate_passes_after_export(tmp_path: Path) -> None:
    in_path = tmp_path / "predictions_live.json"
    in_path.write_text(json.dumps(_feed_with_one_resolved_ko("Argentina",
                                                             "Brazil")))
    out_path = tmp_path / "predictions_live.out.json"

    bracket_path = tmp_path / "bracket.json"
    bracket_path.write_text(json.dumps(
        _bracket_with_one_resolved_r16("Argentina", "Brazil")))
    results_path = _write_min_results(tmp_path)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"groups": {}, "group_stage_schedule": [],
                                    "fifa_rankings_june_2026": {}}))
    annex_path = tmp_path / "annex.json"
    annex_path.write_text(json.dumps({"table": {}}))

    # run_sigma_gate=True is the default; we re-run it externally too to
    # confirm the file on disk is in a passing state.
    export(in_path=in_path, out_path=out_path,
           bracket_path=bracket_path, results_path=results_path,
           cfg_path=cfg_path, annex_c_path=annex_path,
           run_sigma_gate=True)
    check_invariants(out_path)  # must not raise


# --------------------------------------------------------------------------
# Test (4): placeholder-only bracket → empty KO block, Σ-gate passes.
# --------------------------------------------------------------------------
def test_placeholder_only_bracket_empty_ko_block(tmp_path: Path) -> None:
    payload = {
        "team_predictions": _minimal_team_predictions(),
        "match_predictions": [],
        "bracket": _minimal_bracket(),
        "knock_lambdas_table": [],  # sim hasn't run with the hook yet
    }
    in_path = tmp_path / "predictions_live.json"
    in_path.write_text(json.dumps(payload))
    out_path = tmp_path / "predictions_live.out.json"

    bracket_path = tmp_path / "bracket.json"
    bracket_path.write_text(json.dumps(_minimal_bracket()))
    results_path = _write_min_results(tmp_path)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"groups": {}, "group_stage_schedule": [],
                                    "fifa_rankings_june_2026": {}}))
    annex_path = tmp_path / "annex.json"
    annex_path.write_text(json.dumps({"table": {}}))

    result = export(in_path=in_path, out_path=out_path,
                    bracket_path=bracket_path, results_path=results_path,
                    cfg_path=cfg_path, annex_c_path=annex_path)

    assert result.get("match_predictions_ko") == [], (
        "Pre-resolution bracket must produce an empty (NOT missing) "
        "match_predictions_ko block."
    )
    # And Σ-gate must still pass.
    check_invariants(out_path)


# --------------------------------------------------------------------------
# Bonus: skips KO match if λ table absent. Documents the "sim hasn't been
# re-run with the export hook yet" path — same effect as today's reality.
# --------------------------------------------------------------------------
def test_resolved_slots_but_no_lambda_skips_silently(tmp_path: Path) -> None:
    """Both slots resolve but knock_lambdas_table is empty → entry skipped."""
    payload = _feed_with_one_resolved_ko("Argentina", "Brazil")
    payload["knock_lambdas_table"] = []  # drop the λ entry
    predictions = build_ko_advance_entries(
        predictions=payload,
        bracket=payload["bracket"],
        completed_idx={},
        cfg_data={"groups": {}, "group_stage_schedule": [],
                  "fifa_rankings_june_2026": {}},
        annex_c={"table": {}},
    )
    assert predictions == [], (
        "Missing knock_lambdas_table entry must result in skipped (not "
        "errored, not faked-up-with-some-other-source) KO match."
    )


# --------------------------------------------------------------------------
# Bonus: real repo predictions_live.json round-trip. Today this means
# "ko block exists, is empty" — but the test catches a future regression
# where the production feed develops some property we didn't anticipate.
# --------------------------------------------------------------------------
def test_real_repo_predictions_round_trip(tmp_path: Path) -> None:
    src = REPO / "data" / "processed" / "predictions_live.json"
    if not src.exists():
        pytest.skip("real predictions_live.json not present in checkout")
    blob = json.loads(src.read_text())
    in_path = tmp_path / "predictions_live.json"
    in_path.write_text(json.dumps(blob))
    out_path = tmp_path / "predictions_live.out.json"

    # Use the real bracket / annex / cfg from raw, but stub results to
    # empty so the test is hermetic w.r.t. wall-clock state.
    bracket_path = REPO / "data" / "raw" / "knockout_bracket_2026.json"
    annex_path = REPO / "data" / "raw" / "annex_c_third_place_table_2026.json"
    cfg_path = REPO / "data" / "raw" / "wc2026_config.json"
    results_path = _write_min_results(tmp_path)

    result = export(in_path=in_path, out_path=out_path,
                    bracket_path=bracket_path, results_path=results_path,
                    cfg_path=cfg_path, annex_c_path=annex_path)
    # Today: empty (group stage). Future-proof: at minimum, the block exists.
    assert "match_predictions_ko" in result
    assert isinstance(result["match_predictions_ko"], list)
    check_invariants(out_path)
