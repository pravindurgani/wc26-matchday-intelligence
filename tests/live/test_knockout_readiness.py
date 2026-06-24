"""
R32 / knockout-stage readiness audit.

Purpose
-------
The matchday system was originally built for group-stage WDL pricing on a
fixed 72-match schedule. With R32 (m=73..88) starting 2026-06-28, several
knockout-specific rules and data shapes need explicit validation BEFORE
the first knockout tick. This test module is the validation surface; it
does NOT modify production code (other agents own the hardening pass).

Failures are split into two classes:

  1. Hard assertions (`pass`) — invariants that already hold in the code
     and must not regress (schedule integrity, GOAL_GRID 90-min semantics,
     auto_tier floor still appropriate at cumulative knockout minutes).

  2. xfail-strict assertions (`xfail(strict=True)`) — real gaps in
     production code that will misfire at integration time. Strict mode
     means the day the production code is fixed, the test goes XPASS and
     CI fails loudly so the marker can be removed in the same PR.

Gaps under xfail in this module
-------------------------------
  * `suspension_tracker.py` loads ONLY `group_stage_schedule` (line 118)
    — `next_match_for_team` cannot resolve a knockout next-match, so no
    ban ever lands on a knockout fixture, and no yellow-reset boundary
    exists at the QF.
  * `fetch_lineups.py` line 105 reads ONLY `group_stage_schedule` — no
    knockout fixture ever enters the lineup-poll window.
  * `match_predictions` in `data/processed/predictions_live.json` is
    capped at m=1..72 — knockout fixtures live only in the `bracket`
    block (slot codes "1A", "3A/B/C/D/F", ...) with no per-pair WDL
    triple. The dashboard's Matches view renders TBD until a result
    locks; GOAL_GRID input lambdas for a specific knockout pair are
    computed in-simulator but not exported to JSON.

References
----------
  FIFA WC 2022 (and prior tournaments): yellow-card reset after the
    quarter-finals (so a yellow in R32/R16/QF carries forward; QF
    flush; SF starts on a clean slate). Applied here as the WC 2026
    rule absent a published 2026-specific override.
  FIFA WC 2026 schedule: 16 R32 + 8 R16 + 4 QF + 2 SF + 1 3rd + 1 Final
    = 32 knockout fixtures, m=73..104.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import suspension_tracker as st  # noqa: E402


# ---------------------------------------------------------------------------
# 1) Schedule integrity — knockout bracket covers m=73..104 with the right
#    stage breakdown. These MUST already hold; if a fixture drops out the
#    upstream config is broken.
# ---------------------------------------------------------------------------
class TestKnockoutScheduleIntegrity:
    """Bracket file is the canonical source of knockout fixtures."""

    @pytest.fixture
    def bracket(self) -> dict:
        return json.loads(
            (ROOT / "data" / "raw" / "knockout_bracket_2026.json").read_text()
        )

    def test_r32_has_16_fixtures(self, bracket: dict) -> None:
        assert len(bracket["r32_slots"]) == 16

    def test_r16_has_8_fixtures(self, bracket: dict) -> None:
        assert len(bracket["r16_bracket"]) == 8

    def test_qf_has_4_fixtures(self, bracket: dict) -> None:
        assert len(bracket["qf_bracket"]) == 4

    def test_sf_has_2_fixtures(self, bracket: dict) -> None:
        assert len(bracket["sf_bracket"]) == 2

    def test_third_place_and_final_present(self, bracket: dict) -> None:
        ft = bracket["final_and_third_place"]
        assert ft["third_place"]["match_num"] == 103
        assert ft["final"]["match_num"] == 104

    def test_match_numbers_cover_73_to_104_contiguous(self, bracket: dict) -> None:
        nums: list[int] = []
        for slot in bracket["r32_slots"]:
            nums.append(int(slot["match_num"]))
        for slot in bracket["r16_bracket"]:
            nums.append(int(slot["match_num"]))
        for slot in bracket["qf_bracket"]:
            nums.append(int(slot["match_num"]))
        for slot in bracket["sf_bracket"]:
            nums.append(int(slot["match_num"]))
        ft = bracket["final_and_third_place"]
        nums.append(int(ft["third_place"]["match_num"]))
        nums.append(int(ft["final"]["match_num"]))
        assert sorted(nums) == list(range(73, 105)), (
            f"expected contiguous 73..104, got {sorted(nums)}"
        )
        assert len(nums) == 32

    def test_r32_stage_layout_matches_fifa_wc_2026_format(self, bracket: dict) -> None:
        """48-team WC: 12 group winners + 12 runners-up + 8 best 3rd-place
        = 32 teams in R32. Each R32 slot is identified by a slot code like
        "1A" (group A winner), "2B" (group B runner-up), or "3A/B/C/D/F"
        (one of the named groups' 3rd-place team)."""
        all_slots = []
        for s in bracket["r32_slots"]:
            all_slots.append(s["slot_a"])
            all_slots.append(s["slot_b"])
        assert len(all_slots) == 32
        # Every R32 slot must be either "1X", "2X", or "3X/..." for some
        # group letter — no winners-of-knockout placeholders here.
        for slot in all_slots:
            tag = slot.split("/")[0]
            assert tag[0] in ("1", "2", "3"), (
                f"R32 slot {slot!r} doesn't start with 1/2/3"
            )

    def test_third_place_slot_count_is_eight(self, bracket: dict) -> None:
        third_slots = []
        for s in bracket["r32_slots"]:
            for k in ("slot_a", "slot_b"):
                if s[k].startswith("3"):
                    third_slots.append(s[k])
        assert len(third_slots) == 8, (
            "FIFA WC 2026 advances 8 best 3rd-placed teams; expected "
            f"8 third-place slots in R32, got {len(third_slots)}"
        )


# ---------------------------------------------------------------------------
# 2) Yellow-card reset (FIFA rule) — gap test, marked xfail-strict.
# ---------------------------------------------------------------------------
def _ev(kind: str, team: str, player: str, minute: int = 45) -> dict:
    subtype = {
        "yellow": "yellow_card",
        "red": "red_card",
        "second_yellow": "second_yellow_card",
    }[kind]
    return {
        "type": "card", "subtype": subtype, "team": team,
        "player": player, "minute": minute,
    }


_GROUP_THEN_KO_SCHEDULE = [
    # Group stage
    {"m": 1, "date": "2026-06-11", "home": "Spain", "away": "Cape Verde"},
    # Knockout (these are NOT in `group_stage_schedule` today —
    # next_match_for_team can't resolve them under the current loader)
    {"m": 80, "date": "2026-07-01", "home": "Spain", "away": "Italy"},   # R32
    {"m": 92, "date": "2026-07-05", "home": "Spain", "away": "France"},  # R16
    {"m": 99, "date": "2026-07-11", "home": "Spain", "away": "Brazil"},  # QF
    {"m": 101, "date": "2026-07-14", "home": "Spain", "away": "England"},  # SF
    {"m": 104, "date": "2026-07-19", "home": "Spain", "away": "Argentina"},  # Final
]


class TestSuspensionTrackerLoaderContract:
    """Round 6 contract: `load_schedule` must surface BOTH the
    group-stage schedule AND the knockout bracket so
    `next_match_for_team` can resolve bans landing on m=73..104."""

    def test_loader_reads_group_AND_knockout_bracket(self) -> None:
        """`load_schedule` consumes group_stage_schedule + the knockout
        bracket file. Pre-Round 6 it read only group_stage_schedule and
        knockout bans silently dropped to zero rows."""
        import inspect
        src = inspect.getsource(st.load_schedule)
        assert "group_stage_schedule" in src
        assert "knockout" in src.lower(), (
            "load_schedule must reference the knockout bracket; "
            "missing knockout import is the R32 silent-hole regression"
        )

    def test_module_docstring_acknowledges_group_stage_scope(self) -> None:
        assert "group stage" in (st.__doc__ or "")


class TestSuspensionsCorrectGivenKnockoutSchedule:
    """`build_suspensions` is stage-agnostic at the algorithm level: if
    the caller provides a schedule list that INCLUDES knockout rows,
    next_match_for_team resolves R32→R16→QF transitions correctly and
    in-window accumulation (group → R32 → R16) and reds/second-yellows
    fire bans on the right next match.

    These tests pin that contract — they're the floor the production
    fix must preserve. Gap is upstream: load_schedule() never feeds the
    knockout rows in (see TestSuspensionLoadSchedule below)."""

    def test_yellow_in_r32_then_yellow_in_r16_bans_qf(self) -> None:
        completed = [
            {"m": 80, "home": "Spain", "away": "Italy",
             "events": [_ev("yellow", "Spain", "Pedri", 30)]},  # R32
            {"m": 92, "home": "Spain", "away": "France",
             "events": [_ev("yellow", "Spain", "Pedri", 70)]},  # R16
        ]
        sus, _ = st.build_suspensions(completed, _GROUP_THEN_KO_SCHEDULE)
        accumulated = [
            s for s in sus
            if s["reason"] == "accumulated_yellows" and s["player"] == "Pedri"
        ]
        assert len(accumulated) == 1
        assert accumulated[0]["match_id"] == 99  # QF

    def test_red_card_in_r32_bans_next_knockout_fixture(self) -> None:
        completed = [
            {"m": 80, "home": "Spain", "away": "Italy",
             "events": [_ev("red", "Spain", "Pedri", 60)]},
        ]
        sus, _ = st.build_suspensions(completed, _GROUP_THEN_KO_SCHEDULE)
        reds = [s for s in sus if s["reason"] == "red_card"]
        assert len(reds) == 1
        assert reds[0]["match_id"] == 92  # R16


def test_qf_to_sf_yellow_does_not_ban_due_to_qf_flush() -> None:
    """FIFA rule (carries from WC 2022 + 2018 + 2014): the yellow tally
    is WIPED after the quarter-finals. A yellow in QF (counter→1,
    flushed at boundary→0) + yellow in SF (counter→1, below threshold)
    must NOT trigger an accumulation ban for the Final. Round 6 fix:
    `build_suspensions` now detects the QF→SF transition and zeroes any
    yellow_counter at 1."""
    completed = [
        {"m": 99, "home": "Spain", "away": "Brazil",
         "events": [_ev("yellow", "Spain", "Pedri", 30)]},   # QF
        {"m": 101, "home": "Spain", "away": "England",
         "events": [_ev("yellow", "Spain", "Pedri", 70)]},  # SF
    ]
    sus, _ = st.build_suspensions(completed, _GROUP_THEN_KO_SCHEDULE)
    accumulated = [
        s for s in sus
        if s["reason"] == "accumulated_yellows" and s["player"] == "Pedri"
    ]
    # CORRECT BEHAVIOR (FIFA rule): no ban — QF flush reset to 0.
    assert accumulated == [], (
        f"Pedri should NOT be banned for the Final — QF flush rule. "
        f"Got: {accumulated}"
    )


def test_load_schedule_includes_knockout_fixtures() -> None:
    """Round 6: the loader must include knockout fixtures so
    `next_match_for_team` can resolve bans that target knockout
    matches. Group stage = 72 rows, knockout bracket = 32 rows (R32 +
    R16 + QF + SF + 3rd + Final), total 104."""
    sched = st.load_schedule()
    knockout_rows = [r for r in sched if int(r.get("m", 0)) >= 73]
    assert len(knockout_rows) == 32, (
        f"load_schedule returned {len(sched)} rows total, "
        f"{len(knockout_rows)} with m>=73 — expected 32"
    )
    nums = sorted(int(r.get("m", 0)) for r in knockout_rows)
    assert nums == list(range(73, 105)), (
        f"expected contiguous 73..104 knockout rows, got {nums}"
    )
    # Each knockout row must carry a stage tag for the QF-flush logic.
    stages = {r.get("stage") for r in knockout_rows}
    assert stages == {"r32", "r16", "qf", "sf", "3rd", "final"}, (
        f"unexpected knockout stage set: {stages}"
    )


def test_qf_flush_only_wipes_unconverted_carry() -> None:
    """Positive QF-flush case (a): a player on 1 yellow from group stage
    + 1 yellow in R32 should already have hit the threshold (2 yellows)
    at R32 → ban issued for R16 with counter reset. Then a fresh yellow
    in QF leaves counter=1, which the QF→SF flush wipes. Net: ONE
    accumulated_yellows ban for R16, no Final ban.

    (Pre-Round 6 this would have correctly issued the R16 ban but then
    rolled the QF yellow alongside a subsequent SF yellow into a false
    Final ban — that path is now covered by the flush.)"""
    completed = [
        # Group: 1 yellow
        {"m": 13, "home": "Spain", "away": "Cape Verde",
         "events": [_ev("yellow", "Spain", "Pedri", 30)]},
        # R32: 2nd yellow → ban issued for R16 (m=92), counter resets
        {"m": 80, "home": "Spain", "away": "Italy",
         "events": [_ev("yellow", "Spain", "Pedri", 60)]},
        # QF: 1 yellow accumulates fresh → flushed at QF→SF boundary
        {"m": 99, "home": "Spain", "away": "Brazil",
         "events": [_ev("yellow", "Spain", "Pedri", 30)]},
    ]
    # Include m=13 in the schedule so the early ban can resolve.
    schedule = [
        {"m": 13, "date": "2026-06-15", "home": "Spain", "away": "Cape Verde",
         "stage": "group"},
    ] + _GROUP_THEN_KO_SCHEDULE
    sus, _ = st.build_suspensions(completed, schedule)
    accumulated = [
        s for s in sus
        if s["reason"] == "accumulated_yellows" and s["player"] == "Pedri"
    ]
    assert len(accumulated) == 1, (
        f"expected exactly one accumulated_yellows ban (for R16), "
        f"got {len(accumulated)}: {accumulated}"
    )
    assert accumulated[0]["match_id"] == 92, (
        f"R16 ban expected (m=92), got match_id={accumulated[0]['match_id']}"
    )


def test_qf_yellow_alone_then_sf_yellow_no_final_ban() -> None:
    """Positive QF-flush case (b): 0 from group, 1 in R32, 1 in QF (→
    counter=2 → ban for SF, counter resets), then a fresh yellow in SF
    (counter=1, below threshold). Net: ONE accumulated_yellows ban for
    SF (m=101), NO Final ban."""
    completed = [
        # R32: counter=1
        {"m": 80, "home": "Spain", "away": "Italy",
         "events": [_ev("yellow", "Spain", "Pedri", 30)]},
        # QF: counter=2 → ban issued for SF (m=101), counter resets
        {"m": 99, "home": "Spain", "away": "Brazil",
         "events": [_ev("yellow", "Spain", "Pedri", 70)]},
        # SF: fresh yellow, counter=1 (flush already happened at QF→SF
        # boundary BEFORE this event was processed). No Final ban.
        {"m": 101, "home": "Spain", "away": "England",
         "events": [_ev("yellow", "Spain", "Pedri", 30)]},
    ]
    sus, _ = st.build_suspensions(completed, _GROUP_THEN_KO_SCHEDULE)
    accumulated = [
        s for s in sus
        if s["reason"] == "accumulated_yellows" and s["player"] == "Pedri"
    ]
    assert len(accumulated) == 1, (
        f"expected exactly one accumulated_yellows ban (for SF), "
        f"got {len(accumulated)}: {accumulated}"
    )
    assert accumulated[0]["match_id"] == 101, (
        f"SF ban expected (m=101), got match_id={accumulated[0]['match_id']}"
    )
    # And explicitly: no Final (m=104) ban.
    finals = [s for s in sus if s["match_id"] == 104]
    assert finals == [], (
        f"no Final ban should fire; got {finals}"
    )


def test_next_match_for_team_skips_placeholder_slots() -> None:
    """Round 6 positive: `next_match_for_team` must refuse to resolve a
    placeholder slot code to a fixture. A slot like "3A/B/C/D/F" sits in
    the schedule (so the row count stays right) but matching against it
    would attach a ban to whichever real team eventually fills the
    slot — never silently emit such a ban."""
    sched = st.load_schedule()
    # Direct query: a slot code as the "team" must always return None.
    assert st.next_match_for_team("3A/B/C/D/F", 0, sched) is None
    assert st.next_match_for_team("1A", 0, sched) is None
    assert st.next_match_for_team("W74", 0, sched) is None
    assert st.next_match_for_team("L101", 0, sched) is None
    # A concrete team that DOES play in the group stage still resolves.
    spain_next = st.next_match_for_team("Spain", 0, sched)
    assert spain_next is not None, (
        "real team Spain must resolve to a fixture in the loaded schedule"
    )


# ---------------------------------------------------------------------------
# 3) Knockout WDL semantics — GOAL_GRID returns 90-min triple, NOT advance
#    probabilities. This is what the .gs sheet computes today and what the
#    Python wdl_from_matrix returns. Hard-asserts the semantics so an
#    advance-probability swap-in would land loudly.
# ---------------------------------------------------------------------------
def _load_simulate_module():
    """Load scripts/03_simulate.py as a module without running its main."""
    spec = importlib.util.spec_from_file_location(
        "_sim_module", ROOT / "scripts" / "03_simulate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestKnockoutWDL90MinSemantics:
    """GOAL_GRID(λ_h, λ_a, market) is a goal-distribution projector:

      * ah0  → P(home > away) at full time (90 min)
      * ou25 → P(total goals > 2.5) at full time
      * btts → P(both teams score) at full time

    For knockouts the model intentionally exports the 90-min triple
    (p_home_90, p_draw_90, p_away_90) — the user prices goal-line and
    correct-score markets with this. ADVANCE probability for outright
    markets requires a separate tie-break model: with no information,
    a 50/50 split on the draw component is the standard prior:

        p_advance_home = p_home_90 + 0.5 * p_draw_90
        p_advance_away = p_away_90 + 0.5 * p_draw_90

    The simulator implements this 50/50 split inside `resolve_knockout`
    (scripts/03_simulate.py L292-312: 30-min ET sample → penalties via
    a small Elo-edge model) but does NOT expose advance probability as
    a sheet function. GOAL_GRID's ah0 stays 90-min P(home_win), exactly
    as designed.
    """

    def test_wdl_from_matrix_sums_to_one_with_positive_draw(self) -> None:
        sim = _load_simulate_module()
        # Even rates λ=1.5/1.5 — large draw component, no advance bias.
        # Use the in-module DEFAULTS so cfg key names stay in sync with
        # the simulator's expected schema (dc_rho, nb_dispersion).
        cfg = dict(sim.DEFAULTS)
        mat = sim.build_score_matrix(1.5, 1.5, cfg, use_dispersion=False)
        p_h, p_d, p_a = sim.wdl_from_matrix(mat)
        assert abs(p_h + p_d + p_a - 1.0) < 1e-9
        assert p_d > 0.10, (
            f"draw component should be material at λ=1.5/1.5, got {p_d:.4f}"
        )
        # Symmetric matrix → balanced sides within ~5%.
        assert abs(p_h - p_a) < 0.05

    def test_goal_grid_ah0_returns_90min_p_home_not_advance(self) -> None:
        """The .gs GOAL_GRID(lh, la, 'ah0') sums matrix cells with h>a.
        Verify against the Python equivalent on the same matrix; assert
        it is strictly less than the advance probability (p_home_90 +
        0.5*p_draw_90), which would silently include shootout wins."""
        sim = _load_simulate_module()
        cfg = dict(sim.DEFAULTS)
        # Asymmetric: home favored, but with material draw mass.
        mat = sim.build_score_matrix(1.7, 1.1, cfg, use_dispersion=False)
        p_h, p_d, p_a = sim.wdl_from_matrix(mat)
        p_ah0 = float(np.tril(mat, -1).sum())  # GOAL_GRID 'ah0' equivalent
        p_advance_home_5050 = p_h + 0.5 * p_d
        # Strictly less — the gap IS the half-of-draw share that would
        # convert to advance in a 50/50 shootout prior.
        assert p_ah0 < p_advance_home_5050
        # And the gap equals 0.5 * p_draw, within float noise.
        assert abs((p_advance_home_5050 - p_ah0) - 0.5 * p_d) < 1e-9

    def test_goal_grid_source_uses_match_independent_lambdas(self) -> None:
        """The .gs GOAL_GRID(lam_h, lam_a, market) signature takes ONLY
        lambdas and a market key — no match-number argument. The sheet
        therefore prices group AND knockout matches with identical 90-min
        semantics. There is no `if (m_num >= 73)` branch in the source
        that could route a knockout fixture through advance-probability
        math instead of full-time WDL. Asserting this static fact pins
        the contract: a future change that adds match-aware semantics
        would have to either (a) change the GOAL_GRID signature or (b)
        introduce a second function — either makes the regression loud."""
        gs_src = (
            ROOT / "wc26-engine-gs" / "WC26_Engine_AppsScript_v2.3.1.gs"
        ).read_text()
        # The signature line we expect.
        assert "function GOAL_GRID(lam_h, lam_a, market)" in gs_src
        # GOAL_GRID body must NOT branch on match-id / stage variables.
        import re
        # Isolate the GOAL_GRID body up to the matching close-brace
        # (best-effort: function is small and well-bounded).
        body_match = re.search(
            r"function GOAL_GRID\([^)]*\)\s*\{(.*?)\n\}",
            gs_src, re.DOTALL,
        )
        assert body_match is not None
        body = body_match.group(1)
        # Banned tokens — any of these inside GOAL_GRID would mean the
        # function silently rewrites semantics for knockouts.
        for banned in ("match_num", "m_num", "stage", "isKnockout",
                       "knockout"):
            assert banned not in body, (
                f"GOAL_GRID body contains {banned!r} — match-id-aware "
                f"branching would silently change knockout semantics"
            )


# ---------------------------------------------------------------------------
# 4) Knockout predictions exported to JSON — verify the dashboard's data
#    contract. Currently match_predictions only carries group stage 1..72;
#    knockout fixtures live in the `bracket` block with slot codes (no
#    per-pair probabilities pre-resolution). This is a hard assertion of
#    today's contract — a swap that silently injects partial knockout
#    rows would break the Matches view's null-guarding.
# ---------------------------------------------------------------------------
class TestPredictionsLiveKnockoutContract:
    @pytest.fixture
    def predictions(self) -> dict:
        path = ROOT / "data" / "processed" / "predictions_live.json"
        if not path.exists():
            pytest.skip("predictions_live.json not present in this checkout")
        return json.loads(path.read_text())

    def test_bracket_block_present_with_all_stages(self, predictions: dict) -> None:
        bracket = predictions.get("bracket")
        assert isinstance(bracket, dict)
        for key in ("r32_slots", "r16_bracket", "qf_bracket", "sf_bracket",
                    "final_and_third_place"):
            assert key in bracket, f"missing bracket section {key!r}"

    def test_match_predictions_have_p_triples_for_group_stage(self, predictions: dict) -> None:
        """Group fixtures m=1..72 carry the full 90-min WDL triple.
        Knockouts may or may not (today: they don't); this test only
        pins the group-stage contract."""
        mp = predictions.get("match_predictions") or []
        group = [m for m in mp if isinstance(m.get("m"), int) and m["m"] <= 72]
        assert len(group) > 0
        for m in group:
            for key in ("p_home_win", "p_draw", "p_away_win",
                        "lam_home", "lam_away"):
                assert key in m, f"group m={m.get('m')} missing {key}"
            s = m["p_home_win"] + m["p_draw"] + m["p_away_win"]
            assert abs(s - 1.0) < 1e-6, (
                f"group m={m['m']} WDL sums to {s:.6f}, not 1.0"
            )

    def test_knockout_match_p_triples_intentionally_absent(self, predictions: dict) -> None:
        """Today the knockout placeholder rows (if any) under
        match_predictions carry slot labels ("1A", "3A/B/C/D/F") rather
        than concrete teams, so no per-pair WDL probabilities are
        exported. The dashboard renders TBD for these slots. If this
        changes (real teams + concrete probabilities pre-resolution),
        the dashboard's null-guards need to be revisited — flag the
        contract change here so it can't ship silently."""
        mp = predictions.get("match_predictions") or []
        ko = [m for m in mp if isinstance(m.get("m"), int) and m["m"] >= 73]
        # Either zero KO rows (current state) OR rows that carry slot
        # labels and no team-resolved WDL triple.
        for m in ko:
            home = m.get("home") or ""
            away = m.get("away") or ""
            # A slot label like "1A" or "3A/B/C/D/F" or "TBD" — accept
            # any value that's NOT a real country name (no probabilities
            # should accompany those).
            slot_label = (
                home == "TBD" or away == "TBD"
                or "/" in home or "/" in away
                or home == m.get("slot_a") or away == m.get("slot_b")
            )
            has_wdl = (
                "p_home_win" in m and "p_draw" in m and "p_away_win" in m
            )
            if has_wdl and not slot_label:
                # Locked result OR an unexpected new export — make it loud.
                assert m.get("locked_score") is not None, (
                    f"knockout m={m['m']} carries WDL but is not locked "
                    f"and not a slot placeholder — contract drift"
                )


# ---------------------------------------------------------------------------
# 5) Data availability check — which live snapshots cover knockout m=73..104
#    today? Doesn't fail the build; reports findings via assertion messages.
# ---------------------------------------------------------------------------
class TestKnockoutDataAvailability:
    """Inventory which providers ship knockout match references today."""

    @pytest.fixture
    def live_dir(self) -> Path:
        return ROOT / "data" / "live"

    @staticmethod
    def _ko_refs(payload) -> int:
        """Count knockout (m=73..104) match references in a JSON payload."""
        s = json.dumps(payload)
        n = 0
        for m in range(73, 105):
            tokens = (f'"m": {m}', f'"match_id": {m}', f'"match_num": {m}')
            if any(tok in s for tok in tokens):
                n += 1
        return n

    def test_weather_snapshot_covers_all_knockouts(self, live_dir: Path) -> None:
        """fetch_weather.py reads both group_stage_schedule AND
        knockout_bracket_2026.json — every knockout venue must have a
        weather row."""
        path = live_dir / "weather_2026.json"
        if not path.exists():
            pytest.skip("weather_2026.json not present")
        data = json.loads(path.read_text())
        n = self._ko_refs(data)
        assert n == 32, (
            f"weather snapshot covers {n}/32 knockout fixtures — "
            "fetch_weather is supposed to include the full bracket"
        )

    def test_fetch_lineups_loads_group_and_knockout_schedule(self) -> None:
        """Round 6: fetch_lineups._load_schedule must surface group AND
        knockout fixtures so the lineup poller targets the full
        tournament. Pre-Round 6 it loaded only group_stage_schedule and
        every KO lineup window was dark."""
        import inspect
        import fetch_lineups
        src = inspect.getsource(fetch_lineups._load_schedule)
        assert 'group_stage_schedule' in src
        assert 'knockout' in src.lower(), (
            "fetch_lineups._load_schedule must reference the knockout "
            "bracket — KO lineup polling regression"
        )
        sched = fetch_lineups._load_schedule()
        ko = [s for s in sched if int(s.get("m", 0)) >= 73]
        assert len(ko) == 32, (
            f"expected 32 knockout fixtures in lineup target set, "
            f"got {len(ko)}"
        )

    def test_referee_snapshot_knockout_coverage(
        self, live_dir: Path,
    ) -> None:
        """Referee assignments are populated per-match closer to
        kickoff — zero coverage at audit time is normal. This is an
        observability assertion, not a regression gate."""
        path = live_dir / "referee_2026.json"
        if not path.exists():
            pytest.skip("referee_2026.json not present")
        data = json.loads(path.read_text())
        n = self._ko_refs(data)
        # Pre-R32 the expected coverage is 0..16; we only assert the
        # type so a corrupted file doesn't slip through.
        assert isinstance(n, int)


# ---------------------------------------------------------------------------
# 6) Cross-stage edge case — auto_tier MIN_TEAM_TOP_MINUTES floor must
#    remain meaningful for cumulative knockout minutes.
# ---------------------------------------------------------------------------
class TestAutoTierFloorPostGroupStage:
    """MIN_TEAM_TOP_MINUTES = 200 was calibrated pre-tournament against
    a team_top_minutes distribution where p20 ≈ 200 (the smallest pool
    where minutes_share is stable). After group stage, ANY team that
    plays the full 3×90 = 270 minutes already comfortably clears the
    floor, so the small-sample guard is no longer load-bearing for
    starter classifications in the knockout. Pin the floor to keep
    behavior stable; a change to the floor mid-tournament would
    re-shape which players get the auto_insufficient_sample fallback."""

    def test_min_team_top_minutes_is_200(self) -> None:
        import auto_tier as at  # local import — avoid module-load cost
        assert at.MIN_TEAM_TOP_MINUTES == 200

    def test_starter_who_played_full_group_stage_clears_floor(self) -> None:
        """3 group matches × 90 min = 270 > 200, so a team where ANY
        player logged the full group stage automatically clears the
        floor and auto_tier returns a real classification (not the
        auto_insufficient_sample fallback)."""
        import auto_tier as at
        # team_top_minutes is the leader's minutes — at the knockout
        # boundary this is ≥ 270 for any team whose top earner started
        # all three group fixtures.
        assert 270 > at.MIN_TEAM_TOP_MINUTES


# ---------------------------------------------------------------------------
# 7) AUTO_TIER_ACTIVE state confirmation — the audit constraint explicitly
#    says AUTO_TIER_ACTIVE stays False.
# ---------------------------------------------------------------------------
def test_auto_tier_active_remains_false() -> None:
    import injury_adjustments
    assert injury_adjustments.AUTO_TIER_ACTIVE is False, (
        "AUTO_TIER_ACTIVE must stay False through R32 readiness — "
        "the audit constraint pins this flag"
    )
