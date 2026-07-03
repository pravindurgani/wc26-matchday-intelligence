"""
Adversarial CLV tests — Python mirror of the .gs ``refreshCLV`` math.

Background
----------
The Google Apps Script refresher
``wc26-engine-gs/WC26_Engine_AppsScript_v*.gs:refreshCLV`` (latest version)
is hard to unit-test directly (SpreadsheetApp + cell formulas). We mirror the
*observable* arithmetic in Python so we can stress-test the contract:

  CLV% per row  = (E - F) / F     where E = taken_odds, F = closing_odds
  rolling 20-bet = AVERAGE(...) over last CLV_ROLLING_WINDOW rows, ignoring
                   blanks (Sheets AVERAGE behavior); the spreadsheet wraps
                   it in IFERROR(...,"") so any error swallows the window.

Scope
-----
* CLV is **1X2 only** in v2.3.1 (per `_seedCLVHeaders_` note and
  `tests/live/test_goal_grid.py:test_clv_header_note_scopes_to_1x2`).
* Goal-markets are gated by `Method!B8` and explicitly out of CLV scope.

Adversarial matrix — each case is classified LOUD (raises / returns blank /
status pill goes BELOW CLOSE explicitly) or SILENT (returns a confident
number that's actually wrong, OR drops a bad row into the window without
any indication). SILENT cases are marked ``xfail(strict=True)`` so they
turn green the moment the .gs hardens.

These tests do NOT execute the .gs source. They protect the contract by
locking down what the rolling-window math is *supposed* to do at the
boundary cases, and pinning the SILENT bugs we identified so a future
patch can find them.
"""
from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Union

import pytest

# Mirror the constant from WC26_Engine_AppsScript_v*.gs (latest version)
CLV_ROLLING_WINDOW = 20

# Cell-blank sentinel — mirrors how the .gs writes "" into col E/F when
# inputs are missing.
_BLANK = ""

Cell = Union[float, str]  # numeric or "" (blank)


# ---------------------------------------------------------------------------
# Math mirrors (kept deliberately literal — match the .gs formula wiring)
# ---------------------------------------------------------------------------
def odds_from_prob(p: float) -> Cell:
    """Convenience helper for tests that want odds implied by a probability.

    CLV no longer uses model fair odds directly; this helper just keeps the
    numeric test cases readable.
    """
    try:
        if not math.isfinite(p):
            return _BLANK
    except TypeError:
        return _BLANK
    if p <= 0:
        return _BLANK
    return 1.0 / p


def clv_pct_cell(taken_odds: Cell, closing_odds: Cell) -> Cell:
    """Mirror of the per-row formula written at refreshCLV (post-fix).

    Sheets formula (hardened — see refreshCLV):
        =IF(OR(E="",F="",NOT(ISNUMBER(E)),NOT(ISNUMBER(F)),E<=0,F<=0),"", (E-F)/F)

    The F<=0 gate pre-validates closing_odds so a bad operator entry
    surfaces as a blank CLV cell rather than #DIV/0! cascading through
    the IFERROR-wrapped rolling-window and collapsing the entire window.
    """
    if taken_odds == _BLANK or closing_odds == _BLANK:
        return _BLANK
    try:
        f = float(closing_odds)
        e = float(taken_odds)
    except (TypeError, ValueError):
        # Sheets NOT(ISNUMBER(F)) gate fires → CLV cell is blank.
        return _BLANK
    # Pre-validated E/F>0: non-positive odds become blank, NOT
    # an error token. This is the post-fix behavior at refreshCLV.
    if not math.isfinite(e) or e <= 0 or not math.isfinite(f) or f <= 0:
        return _BLANK
    return (e - f) / f


def rolling_avg_cell(window: Sequence[Cell]) -> Cell:
    """Mirror of refreshCLV rolling window (post-fix, refreshCLV).

    Sheets formula (hardened):
        =IFERROR(
          IF(COUNT(G(winStart):G(r))<CLV_ROLLING_WINDOW,
             "n="&COUNT(...)&": "&AVERAGE(...),
             AVERAGE(...)),
          "")

    Returns either a bare float (n>=CLV_ROLLING_WINDOW — the full window
    is populated) or a string ``"n=K: <avg>"`` for partial windows. The
    n-aware label prevents the operator mistaking a 3-bet rolling avg
    for a 20-bet edge signal.

    AVERAGE in Sheets ignores text and blanks; error tokens (#DIV/0!,
    #VALUE!, #NUM!) propagate and IFERROR collapses the whole window to "".
    """
    numeric: List[float] = []
    for cell in window:
        if cell == _BLANK:
            continue
        if isinstance(cell, str):
            # Error tokens (#DIV/0!, #VALUE!, #NUM!) propagate via AVERAGE.
            if cell.startswith("#"):
                return _BLANK
            # Other strings ignored by AVERAGE.
            continue
        if isinstance(cell, float) and math.isnan(cell):
            # Sheets does not natively yield NaN, but if an upstream cell
            # somehow surfaces NaN treat it as #NUM! → window collapses.
            return _BLANK
        numeric.append(float(cell))
    if not numeric:
        return _BLANK
    avg = sum(numeric) / len(numeric)
    if len(numeric) < CLV_ROLLING_WINDOW:
        return f"n={len(numeric)}: {avg}"
    return avg


def status_pill(rolling: Cell) -> str:
    """Mirror of refreshCLV status-pill formula (post-fix).

    Handles both the bare-float full-window output and the partial-window
    ``"n=K: <avg>"`` label by extracting the numeric tail and tagging the
    pill with ``(n<CLV_ROLLING_WINDOW)`` so the operator can't mistake a
    3-bet edge for a 20-bet edge signal.
    """
    if rolling == _BLANK:
        return ""
    if isinstance(rolling, str):
        # Partial-window label: "n=K: <avg>"
        if ": " in rolling:
            try:
                tail = rolling.split(": ", 1)[1]
                v = float(tail)
            except (ValueError, IndexError):
                return ""
            beat = v > 0
            return (
                f"BEATING CLOSE (n<{CLV_ROLLING_WINDOW})"
                if beat
                else f"BELOW CLOSE (n<{CLV_ROLLING_WINDOW})"
            )
        return ""
    try:
        return "BEATING CLOSE" if float(rolling) > 0 else "BELOW CLOSE"
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Pick-scope gate — mirrors the Apps Script _isOneXTwoPick_ whitelist.
# ---------------------------------------------------------------------------
_VALID_1X2 = {
    "H", "HOME", "1", "HOME WIN",
    "D", "DRAW", "X",
    "A", "AWAY", "2", "AWAY WIN",
}


def pick_in_1x2_scope(pick: str) -> bool:
    return pick.strip().upper() in _VALID_1X2


# ---------------------------------------------------------------------------
# Happy path — sanity check the mirror itself
# ---------------------------------------------------------------------------
def test_happy_path_positive_clv() -> None:
    """Taken odds 2.50, closing settles at 2.20 → CLV ≈ +0.1364.

    n=1 < CLV_ROLLING_WINDOW so the status pill tags the partial window.
    """
    e = odds_from_prob(0.40)  # 2.50
    clv = clv_pct_cell(e, 2.20)
    assert clv == pytest.approx((2.5 - 2.2) / 2.2)
    pill = status_pill(rolling_avg_cell([clv]))
    assert pill == f"BEATING CLOSE (n<{CLV_ROLLING_WINDOW})"


def test_happy_path_negative_clv() -> None:
    """Taken odds 2.00, closing 2.20 → CLV ≈ −0.0909 (below close).

    n=1 < CLV_ROLLING_WINDOW so the partial-window tag fires.
    """
    e = odds_from_prob(0.50)
    clv = clv_pct_cell(e, 2.20)
    assert clv < 0
    pill = status_pill(rolling_avg_cell([clv]))
    assert pill == f"BELOW CLOSE (n<{CLV_ROLLING_WINDOW})"


# ---------------------------------------------------------------------------
# Closing-odds boundary cases
# ---------------------------------------------------------------------------
def test_closing_odds_zero_yields_blank_clv_cell() -> None:
    """FIXED (was 'yields #DIV/0!'): post-patch refreshCLV now
    pre-validates F>0 in the per-row formula (NOT(ISNUMBER(F)) or F<=0
    short-circuits to ''). So a 0.0 closing odds yields a blank CLV cell,
    NOT a #DIV/0! error token. The rolling-window average then aggregates
    the 19 good rows cleanly instead of being collapsed by IFERROR.
    """
    e = odds_from_prob(0.40)
    clv = clv_pct_cell(e, 0.0)
    assert clv == _BLANK, (
        f"closing_odds=0 must yield blank CLV cell after pre-validation, "
        f"got {clv!r}"
    )
    # Mix one bad row with 19 good rows — the 19 good rows still aggregate.
    good_val = clv_pct_cell(odds_from_prob(0.40), 2.20)
    window = [good_val] * 19 + [clv]
    out = rolling_avg_cell(window)
    # n=19 < CLV_ROLLING_WINDOW → partial-window label fires.
    assert isinstance(out, str) and out.startswith("n=19: "), (
        f"expected partial-window label 'n=19: <avg>', got {out!r}"
    )


def test_closing_odds_zero_no_longer_collapses_rolling_window() -> None:
    """FIXED (was xfail): closing_odds=0 used to produce #DIV/0! that
    silently collapsed the rolling window via IFERROR — operator saw a
    blank status pill instead of a meaningful warning. Post-patch the
    per-row CLV cell is blank (not an error), the rolling AVERAGE skips
    it like any other blank, and the partial-window n-aware label
    surfaces the actual sample size.
    """
    e = odds_from_prob(0.40)
    clv = clv_pct_cell(e, 0.0)
    # The bad row is blank — rolling window still aggregates other rows.
    good_val = clv_pct_cell(odds_from_prob(0.40), 2.20)
    window = [good_val, clv, good_val]
    out = rolling_avg_cell(window)
    # n=2 effective → string label fires; the average is non-blank and
    # reflects the GOOD rows only (bad row contributes nothing).
    assert out != _BLANK, (
        "post-fix: bad closing_odds=0 should not collapse the rolling "
        "window — average of the 2 good rows must still appear."
    )
    assert isinstance(out, str) and out.startswith("n=2: ")


def test_closing_odds_text_inf_yields_blank_after_isnumber_gate() -> None:
    """FIXED: post-patch the per-row formula's NOT(ISNUMBER(F)) gate
    rejects text-as-odds before division can fire. Python mirror parses
    'inf' to a float (Python is more permissive than Sheets), so the
    F<=0 gate doesn't catch it — but isfinite() does, and the cell goes
    blank. Real Sheets refuses 'inf' as text via NOT(ISNUMBER(F))."""
    e = odds_from_prob(0.40)
    clv = clv_pct_cell(e, "inf")
    assert clv == _BLANK, (
        f"expected blank CLV cell for 'inf' (non-finite), got {clv!r}"
    )
    assert rolling_avg_cell([clv]) == _BLANK


def test_closing_odds_truly_unparseable_text_yields_blank() -> None:
    """FIXED: post-patch unparseable text → NOT(ISNUMBER(F)) fires → cell
    is blank. No error token, no IFERROR-collapsed rolling window."""
    e = odds_from_prob(0.40)
    clv = clv_pct_cell(e, "abc")
    assert clv == _BLANK, (
        f"expected blank CLV cell for 'abc' (unparseable), got {clv!r}"
    )
    assert rolling_avg_cell([clv]) == _BLANK


def test_closing_implied_prob_equal_one_no_juice() -> None:
    """closing_odds = 1.0 (implied prob = 100%) → degenerate but arithmetic
    is still defined. CLV = E - 1. Recorded silently — operator's problem.
    Status pill carries the n<CLV_ROLLING_WINDOW tag because n=1."""
    e = odds_from_prob(0.40)  # 2.50
    clv = clv_pct_cell(e, 1.0)
    assert clv == pytest.approx(1.5)  # numerically valid, semantically nuts
    assert (
        status_pill(rolling_avg_cell([clv]))
        == f"BEATING CLOSE (n<{CLV_ROLLING_WINDOW})"
    )


def test_closing_implied_prob_above_one_negative_juice() -> None:
    """closing_odds = 0.95 (implied prob 105%) → arb / book error. No
    validation; treated as ordinary number. SILENT. Status pill carries
    the n<CLV_ROLLING_WINDOW tag because n=1."""
    e = odds_from_prob(0.40)
    clv = clv_pct_cell(e, 0.95)
    # CLV is positive (and inflated) — no warning fires.
    assert clv > 0
    assert (
        status_pill(rolling_avg_cell([clv]))
        == f"BEATING CLOSE (n<{CLV_ROLLING_WINDOW})"
    )


# ---------------------------------------------------------------------------
# Taken-odds boundary cases
# ---------------------------------------------------------------------------
def test_taken_odds_blank_yields_blank_clv() -> None:
    """LOUD: missing taken odds → downstream CLV cell and pill are blank."""
    assert clv_pct_cell(_BLANK, 2.20) == _BLANK


def test_taken_odds_one_degenerate_but_numeric() -> None:
    """Taken odds = 1.0. CLV = (1 - closing)/closing. Numerics fine."""
    assert clv_pct_cell(1.0, 2.20) < 0  # taking 1.0 on a 2.20 close is bad


def test_taken_odds_nan_yields_blank_clv() -> None:
    """LOUD: NaN is gated by the finite-number mirror."""
    assert clv_pct_cell(float("nan"), 2.20) == _BLANK


def test_taken_odds_negative_yields_blank_clv() -> None:
    """LOUD: odds <= 0 is gated before CLV division."""
    assert clv_pct_cell(-0.05, 2.20) == _BLANK


# ---------------------------------------------------------------------------
# Rolling-window behavior
# ---------------------------------------------------------------------------
def test_rolling_window_with_only_one_bet() -> None:
    """FIXED: post-patch refreshCLV wraps the rolling AVERAGE
    with an IF(COUNT(window)<CLV_ROLLING_WINDOW, "n=K: <avg>", AVERAGE())
    branch. So a 1-cell window now emits 'n=1: <avg>' as a STRING — the
    operator can't mistake a single-sample partial window for a full
    20-bet edge signal. The numeric value is recoverable from the tail."""
    clv = clv_pct_cell(odds_from_prob(0.40), 2.20)
    out = rolling_avg_cell([clv])
    assert isinstance(out, str), (
        f"expected partial-window string label for n=1, got {out!r}"
    )
    assert out.startswith("n=1: ")
    # The numeric tail must still round-trip to the underlying CLV.
    tail = float(out.split(": ", 1)[1])
    assert tail == pytest.approx(clv)


def test_rolling_window_one_bet_flags_insufficient_sample() -> None:
    """FIXED (was xfail): post-patch the rolling window emits an n-aware
    label whenever COUNT < CLV_ROLLING_WINDOW. So a 1-bet window is no
    longer a bare float that masquerades as a 20-bet edge."""
    clv = clv_pct_cell(odds_from_prob(0.40), 2.20)
    out = rolling_avg_cell([clv])
    assert not isinstance(out, float), (
        f"post-fix: n=1 window must NOT return a bare float, got {out!r}"
    )
    assert isinstance(out, str) and out.startswith("n=1: ")


def test_negative_clv_streak_over_20_no_clipping() -> None:
    """LOUD-correct: 25 consecutive losses to the close → rolling avg
    stays negative (last 20 used). No clipping at zero."""
    losing_bets = [
        clv_pct_cell(odds_from_prob(0.50), 2.50)  # took 2.0 < close 2.5
        for _ in range(25)
    ]
    # Slice the window to the last CLV_ROLLING_WINDOW per refreshCLV.
    window = losing_bets[-CLV_ROLLING_WINDOW:]
    avg = rolling_avg_cell(window)
    assert isinstance(avg, float)
    assert avg < 0
    assert status_pill(avg) == "BELOW CLOSE"


def test_rolling_window_blanks_ignored() -> None:
    """AVERAGE skips blanks (per Sheets semantics). Confirm mirror matches.
    n=2 effective < CLV_ROLLING_WINDOW → partial-window label fires."""
    good = clv_pct_cell(odds_from_prob(0.40), 2.20)
    window = [good, _BLANK, _BLANK, good]
    out = rolling_avg_cell(window)
    assert isinstance(out, str) and out.startswith("n=2: ")
    tail = float(out.split(": ", 1)[1])
    assert tail == pytest.approx(good)


def test_rolling_window_text_ignored() -> None:
    """AVERAGE skips text (non-error). Confirm mirror matches.
    n=2 effective < CLV_ROLLING_WINDOW → partial-window label fires."""
    good = clv_pct_cell(odds_from_prob(0.40), 2.20)
    window = [good, "pending", good]
    out = rolling_avg_cell(window)
    assert isinstance(out, str) and out.startswith("n=2: ")
    tail = float(out.split(": ", 1)[1])
    assert tail == pytest.approx(good)


# ---------------------------------------------------------------------------
# Scope: 1X2 only — picks outside scope must be excluded from CLV
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pick",
    ["H", "D", "A", "HOME", "DRAW", "AWAY", "HOME WIN", "AWAY WIN", "1", "X", "2", "x"],
)
def test_1x2_picks_in_scope(pick: str) -> None:
    assert pick_in_1x2_scope(pick)


@pytest.mark.parametrize(
    "pick",
    ["Over 2.5", "Under 2.5", "BTTS Yes", "BTTS No", "Correct Score 2-1", ""],
)
def test_non_1x2_picks_out_of_scope(pick: str) -> None:
    """Goal-markets / BTTS / correct-score picks are out of CLV scope in
    v2.3.1. The .gs documents this in _seedCLVHeaders_ note (B1)."""
    assert not pick_in_1x2_scope(pick)


def test_out_of_scope_pick_is_pre_filtered() -> None:
    """FIXED (was xfail): post-patch refreshCLV now filters
    `if (!_isOneXTwoPick_(pick)) continue;` BEFORE pushing to placedRows.
    Out-of-scope picks (O/U, BTTS, Correct Score) no longer occupy CLV
    rows with blank taken_odds / inflate the placed-bet count in the
    status message.

    We assert the post-fix behavior: an Over 2.5 pick passed through
    the placed-rows collector is dropped entirely.
    """
    assert not pick_in_1x2_scope("Over 2.5")
    bets = [
        {"m": 7, "snap_decision": "BET", "snap_pick": "Over 2.5",
         "snap_stake": 100},
    ]
    placed = _placed_rows_from_bets(bets)
    assert placed == [], (
        "post-fix: out-of-scope Over 2.5 pick must be excluded from "
        "placedRows entirely, not appear with blank taken_odds. "
        f"Got placed={placed!r}"
    )


# ---------------------------------------------------------------------------
# Dedup / idempotency — same (#m, pick) twice
# ---------------------------------------------------------------------------
def _placed_rows_from_bets(bets: Iterable[dict]) -> List[dict]:
    """Mirror of refreshCLV — collect placed rows from Bets snapshots + odds.

    Post-fix invariants (refreshCLV.gs hardening):
      * 1X2-scope gate: out-of-scope picks (O/U, BTTS, CS) are filtered
        BEFORE placedRows.push so they don't inflate the placed-bet count
        with non-1X2 rows.
      * Dedup on (#m, pick): duplicate Bets rows for the same key are
        collapsed (first occurrence wins) so the rolling avg can't
        silently double-count the same bet.

    A row is 'placed' if snapPick is in the 1X2 whitelist AND (snapDecision
    starts with BET or equals YES, OR snapDecision is empty).
    """
    out: List[dict] = []
    seen: set = set()
    for b in bets:
        pick = (b.get("snap_pick") or "").strip()
        if not pick:
            continue
        # 1X2 scope gate — bug #5.
        if not pick_in_1x2_scope(pick):
            continue
        dec = (b.get("snap_decision") or "").strip().upper()
        if dec and "BET" not in dec and dec != "YES":
            continue
        m = b.get("m")
        if not isinstance(m, (int, float)) or not math.isfinite(m):
            continue
        # Dedup on (#m, pick) — bug #4.
        key = f"{m}|{pick.upper()}"
        if key in seen:
            continue
        seen.add(key)
        backed = _positive_odds(b.get("backed_odds"))
        picked = _positive_odds(b.get("picked_odds"))
        out.append({
            "m": m,
            "pick": pick,
            "stake": b.get("snap_stake"),
            "taken_odds": backed if backed != _BLANK else picked,
        })
    return out


def _positive_odds(v: object) -> Cell:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return _BLANK
    return x if math.isfinite(x) and x > 0 else _BLANK


def test_taken_odds_prefers_logged_backed_odds() -> None:
    bets = [
        {
            "m": 7,
            "snap_decision": "BET",
            "snap_pick": "H",
            "snap_stake": 100,
            "backed_odds": 2.60,
            "picked_odds": 2.70,
        },
    ]
    placed = _placed_rows_from_bets(bets)
    assert placed[0]["taken_odds"] == pytest.approx(2.60)


def test_taken_odds_falls_back_to_engine_pick_for_legacy_rows() -> None:
    bets = [
        {
            "m": 7,
            "snap_decision": "BET",
            "snap_pick": "H",
            "snap_stake": 100,
            "backed_odds": "",
            "picked_odds": 2.70,
        },
    ]
    placed = _placed_rows_from_bets(bets)
    assert placed[0]["taken_odds"] == pytest.approx(2.70)


def test_dedup_same_m_pick_collapses_after_fix() -> None:
    """FIXED: post-patch refreshCLV maintains a seenKeys Set
    keyed on (#m, pick). Duplicate Bets rows for the same key are
    collapsed (first occurrence wins) so the rolling avg can no longer
    silently double-count the same bet."""
    bets = [
        {"m": 7, "snap_decision": "BET", "snap_pick": "H", "snap_stake": 100},
        {"m": 7, "snap_decision": "BET", "snap_pick": "H", "snap_stake": 100},
    ]
    placed = _placed_rows_from_bets(bets)
    assert len(placed) == 1, (
        f"refreshCLV must dedup (#m, pick) duplicates; got {placed!r}"
    )
    assert placed[0]["m"] == 7
    assert placed[0]["pick"] == "H"


def test_dedup_collapses_duplicate_m_pick() -> None:
    """FIXED (was xfail): duplicate (#m, pick) rows used to be written to
    CLV twice and silently double-counted in the rolling avg. Post-fix
    they collapse to a single placed-row entry."""
    bets = [
        {"m": 7, "snap_decision": "BET", "snap_pick": "H", "snap_stake": 100},
        {"m": 7, "snap_decision": "BET", "snap_pick": "H", "snap_stake": 100},
    ]
    placed = _placed_rows_from_bets(bets)
    assert len(placed) == 1, (
        f"expected dedup to collapse same-key duplicate, got {placed!r}"
    )


def test_dedup_does_not_collapse_distinct_picks_for_same_match() -> None:
    """Dedup is keyed on (#m, pick), NOT just #m — two genuinely distinct
    1X2 picks for the same match (e.g. H and D) must both be kept."""
    bets = [
        {"m": 7, "snap_decision": "BET", "snap_pick": "H", "snap_stake": 100},
        {"m": 7, "snap_decision": "BET", "snap_pick": "D", "snap_stake": 50},
    ]
    placed = _placed_rows_from_bets(bets)
    assert len(placed) == 2, (
        f"distinct picks (H, D) for same #m must NOT be deduped; got {placed!r}"
    )


def test_idempotency_rerun_preserves_operator_typed_close() -> None:
    """LOUD-correct: refreshCLV is documented idempotent
    'matches existing rows by (#m, pick) so the operator's typed closing
    odds survive subsequent refreshes'). Confirm mirror reflects that."""
    # Mirror: dict keyed by (#m, pick) — re-running rebuilds rows but reads
    # closingByKey to retain operator-typed close. We just assert the
    # contract: the same key from a prior write surfaces on rebuild.
    prior_close: dict = {("7", "H"): 2.20}
    new_rows = [{"m": 7, "pick": "H"}]
    rebuilt_close = [
        prior_close.get((str(r["m"]), r["pick"].upper()), _BLANK)
        for r in new_rows
    ]
    assert rebuilt_close == [2.20]


# ---------------------------------------------------------------------------
# Stake-edge cases — stake doesn't affect CLV math, but contract should
# at least not silently treat 0/negative as a normal placed bet.
# ---------------------------------------------------------------------------
def test_stake_zero_still_records_bet() -> None:
    """SILENT-NEUTRAL: stake=0 is recorded as a placed bet. refreshCLV does
    not use stake in the CLV formula so the average is unaffected, but the
    sheet shows a 0-stake row. Documents the behavior."""
    bets = [
        {"m": 7, "snap_decision": "BET", "snap_pick": "H", "snap_stake": 0},
    ]
    placed = _placed_rows_from_bets(bets)
    assert len(placed) == 1
    assert placed[0]["stake"] == 0


def test_stake_negative_still_records_bet() -> None:
    """Same as above — refreshCLV does not gate on stake sign."""
    bets = [
        {"m": 7, "snap_decision": "BET", "snap_pick": "H", "snap_stake": -50},
    ]
    placed = _placed_rows_from_bets(bets)
    assert len(placed) == 1
    assert placed[0]["stake"] == -50


# ---------------------------------------------------------------------------
# Snapshot-block scope check — confirms placement gate
# ---------------------------------------------------------------------------
def test_empty_pick_excluded() -> None:
    """LOUD: empty snapPick → row skipped."""
    bets = [
        {"m": 7, "snap_decision": "BET", "snap_pick": "", "snap_stake": 100},
    ]
    assert _placed_rows_from_bets(bets) == []


def test_non_bet_decision_excluded() -> None:
    """LOUD: decision='SKIP' → row skipped."""
    bets = [
        {"m": 7, "snap_decision": "SKIP", "snap_pick": "H", "snap_stake": 100},
    ]
    assert _placed_rows_from_bets(bets) == []


def test_non_finite_match_no_excluded() -> None:
    """LOUD: m=NaN → row skipped."""
    bets = [
        {"m": float("nan"), "snap_decision": "BET", "snap_pick": "H"},
    ]
    assert _placed_rows_from_bets(bets) == []
