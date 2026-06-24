"""
R9 P1 regression — lookup_third_place_assignment must return None on
partial annex_c corruption, NOT raise an opaque KeyError that bypasses
the R7 N1 friendly diagnostic.

Pre-R9 the lookup did `out[mapping[slot_key]] = q` which raised
KeyError synchronously when the annex_c table key existed but its inner
"3X" mapping was missing one of the eight required slot keys (truncated
file, bad merge, partial-write corruption). The R7 N1 RuntimeError
at scripts/03_simulate.py:466-471 only fires inside `if third_slot_map
is None:`, so a partial-key corruption tore down the seed mid-loop
with a useless KeyError instead of surfacing the actionable
"check data/raw/annex_c_thirds_map.json" message R7 N1 added.

R9 P1 (scripts/03_simulate.py:359-381): wrap the inner lookup in
`mapping.get(slot_key)` and return None on miss — partial corruption
now flows through the same R7 N1 fallback path as a full-table miss.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location(
    "simulate_module", ROOT / "scripts" / "03_simulate.py"
)
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)


_QUALIFIERS = [
    {"name": "TeamA", "group": "A"},
    {"name": "TeamB", "group": "B"},
    {"name": "TeamC", "group": "C"},
    {"name": "TeamD", "group": "D"},
    {"name": "TeamE", "group": "E"},
    {"name": "TeamF", "group": "F"},
    {"name": "TeamG", "group": "G"},
    {"name": "TeamH", "group": "H"},
]


class TestLookupThirdPlaceAssignment(unittest.TestCase):
    def test_happy_path_returns_full_mapping(self):
        """Sanity: a complete annex_c entry returns an 8-slot dict."""
        full_table = {
            "ABCDEFGH": {
                "3A": "M74", "3B": "M75", "3C": "M76", "3D": "M77",
                "3E": "M78", "3F": "M79", "3G": "M80", "3H": "M81",
            }
        }
        out = sim.lookup_third_place_assignment(_QUALIFIERS, full_table)
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 8)
        self.assertEqual(out["M74"]["name"], "TeamA")
        self.assertEqual(out["M81"]["name"], "TeamH")

    def test_full_key_miss_returns_none(self):
        """Pre-R9 behaviour preserved: table missing the joined-group key
        returns None, triggering the R7 N1 fallback."""
        empty_table = {}
        out = sim.lookup_third_place_assignment(_QUALIFIERS, empty_table)
        self.assertIsNone(out)

    def test_r9_p1_partial_key_corruption_returns_none(self):
        """The R9 P1 closure: table HAS the joined-group key but is internally
        inconsistent (missing one or more 3X slot mappings). Pre-R9 this
        raised KeyError synchronously, bypassing the R7 N1 RuntimeError
        diagnostic. Post-R9 must return None so the fallback path fires."""
        # Table has the key but slot "3H" mapping is missing — exactly the
        # truncated-file / partial-write corruption shape.
        partial_table = {
            "ABCDEFGH": {
                "3A": "M74", "3B": "M75", "3C": "M76", "3D": "M77",
                "3E": "M78", "3F": "M79", "3G": "M80",
                # "3H" deliberately omitted
            }
        }
        # Must NOT raise. Must return None.
        out = sim.lookup_third_place_assignment(_QUALIFIERS, partial_table)
        self.assertIsNone(out,
            "R9 P1: partial-key corruption must return None so the R7 N1 "
            "friendly diagnostic fires, not raise an opaque KeyError that "
            "tears down the seed mid-loop")

    def test_r9_p1_partial_key_corruption_first_slot_missing(self):
        """Same defense against an early-slot omission (not just the last)."""
        partial_table = {
            "ABCDEFGH": {
                # "3A" deliberately omitted — would have raised at the first
                # qualifier in pre-R9 code.
                "3B": "M75", "3C": "M76", "3D": "M77",
                "3E": "M78", "3F": "M79", "3G": "M80", "3H": "M81",
            }
        }
        out = sim.lookup_third_place_assignment(_QUALIFIERS, partial_table)
        self.assertIsNone(out)

    def test_r9_p1_completely_empty_inner_mapping_returns_none(self):
        """Worst case: key present, inner dict is empty."""
        partial_table = {"ABCDEFGH": {}}
        out = sim.lookup_third_place_assignment(_QUALIFIERS, partial_table)
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
