"""Static checks for the Apps Script `extendToKnockouts()` extension.

We can't run Apps Script in CI, so these tests parse the .gs source and
assert:
  - the function exists
  - knockout layout constants cover 32 fixtures (m=73..104)
  - stages sum to 32 with the FIFA WC 2026 breakdown (16+8+4+2+1+1)
  - the menu item is wired to the function name
  - BC:BE seed formulas mirror I:K (matches v2.3.1 CRIT #3 pattern)
"""
from __future__ import annotations

import re
from pathlib import Path

GS = (Path(__file__).resolve().parents[2]
      / "wc26-engine-gs" / "WC26_Engine_AppsScript_v2.3.1.gs")


def _src() -> str:
    return GS.read_text()


def test_function_defined():
    assert "function extendToKnockouts()" in _src()


def test_menu_item_wired():
    src = _src()
    # Menu addItem('label', 'extendToKnockouts')
    pat = re.compile(
        r"\.addItem\(\s*'[^']*knockout[^']*'\s*,\s*'extendToKnockouts'\s*\)",
        re.IGNORECASE,
    )
    assert pat.search(src), "menu item for extendToKnockouts not found"


def test_knockout_window_constants():
    src = _src()
    assert "const KNOCKOUT_FIRST_M = 73;" in src
    assert "const KNOCKOUT_LAST_M  = 104;" in src
    # Row mapping: row = BETS_FIRST_DATA_ROW + (m - 1); group rows are 2..73
    # so knockout rows must be 74..105.
    assert "KNOCKOUT_FIRST_M - 1" in src
    assert "KNOCKOUT_LAST_M  - 1" in src


def test_stage_layout_covers_fifa_wc_breakdown():
    """FIFA WC 2026 knockouts: 16 R32 + 8 R16 + 4 QF + 2 SF + 1 3rd + 1 Final = 32."""
    src = _src()
    # Extract each stage row from KNOCKOUT_STAGES = [ ... ]
    block_match = re.search(
        r"const KNOCKOUT_STAGES\s*=\s*\[(.*?)\];", src, re.DOTALL,
    )
    assert block_match, "KNOCKOUT_STAGES array not found"
    block = block_match.group(1)
    rows = re.findall(
        r"\{\s*stage:\s*'([^']+)'\s*,\s*first:\s*(\d+)\s*,\s*last:\s*(\d+)\s*\}",
        block,
    )
    expected = {
        "R32":   (73,  88),  # 16
        "R16":   (89,  96),  # 8
        "QF":    (97,  100),  # 4
        "SF":    (101, 102),  # 2
        "3rd":   (103, 103),  # 1
        "Final": (104, 104),  # 1
    }
    actual = {name: (int(f), int(l)) for name, f, l in rows}
    assert actual == expected
    total = sum(l - f + 1 for f, l in expected.values())
    assert total == 32, f"stage layout covers {total} matches, expected 32"


def test_bc_be_seed_formulas_mirror_ijk():
    """v2.3.1 CRIT #3: BC:BE seeded with =I{r}/=J{r}/=K{r}."""
    src = _src()
    # The extension must emit the same mirror pattern for new rows.
    assert "'=I' + r" in src
    assert "'=J' + r" in src
    assert "'=K' + r" in src


def test_idempotent_contract_documented():
    src = _src()
    # The docstring must promise idempotency — re-running on a populated
    # spreadsheet must not double-add rows.
    block_match = re.search(
        r"// KNOCKOUT EXTENSION.*?function extendToKnockouts\(\)",
        src, re.DOTALL,
    )
    assert block_match, "knockout section header not found"
    header = block_match.group(0)
    assert "idempotent" in header.lower() or "re-running is safe" in header.lower()


def test_conflict_branch_does_not_overwrite():
    """If a knockout-window row already holds a non-matching match_no, we
    must preserve it (the operator may have manually mapped a fixture)."""
    src = _src()
    # The conflict counter and the "non-knockout match_no" warning string
    # both have to be present.
    assert "conflicts++" in src
    assert "CONFLICT" in src
