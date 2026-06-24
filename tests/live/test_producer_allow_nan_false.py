"""
R9 P3 regression — every producer writer feeding apply_matchday_adjustments
must use allow_nan=False. Pre-R9 only the matchday_intelligence.json
writer at apply_matchday_adjustments.py:101 (R8 O2) rejected NaN/Infinity;
the 7+ upstream producers all used the json.dump default allow_nan=True,
so a NaN in any subsystem's `data/live/*_2026.json` would silently
round-trip through json (CPython's `json.loads("NaN")` → `nan`),
propagate into the matchday aggregator's math, and reach the R8 O2
boundary AFTER having already poisoned the upstream file on disk.
Next tick would re-read the NaN from disk and the crash would repeat —
requires manual file deletion to recover.

R9 P3 closes the fig-leaf: every json.dump call in producer paths now
carries allow_nan=False so NaN/Inf fails LOUDLY at the producer write
boundary, not after the upstream file is already corrupted.

This test is intentionally a static pin (not behavioural) — the
producers each have their own behavioural tests; the static pin catches
the "revert to default" footgun: a future contributor copy-pastes
another atomic_write helper without allow_nan=False and the suite fails
immediately.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Producer writers whose JSON output is consumed downstream by either
# apply_matchday_adjustments aggregator OR the simulator OR the dashboard.
# Map: file_path → list of (line_hint, function_name_hint).
PRODUCERS_REQUIRING_ALLOW_NAN_FALSE = [
    "scripts/live/fetch_results.py",
    "scripts/live/fetch_player_stats.py",
    "scripts/live/referee_adjustments.py",
    "scripts/live/fetch_injuries.py",
    "scripts/live/fetch_match_stats.py",
    "scripts/live/suspension_tracker.py",
    "scripts/live/fetch_weather.py",
    "scripts/live/fetch_lineups.py",
    "scripts/live/export_ko_advance.py",
    "scripts/live/run_live_update.py",
    "scripts/live/apply_matchday_adjustments.py",
    "scripts/03_simulate.py",
]


class TestProducerAllowNanFalse(unittest.TestCase):
    def test_every_boundary_writer_uses_allow_nan_false(self):
        """For each producer file, the boundary-write json.dump( (and
        the audit-log f.write(json.dumps(...)) variant) must carry
        allow_nan=False. Hash-cache json.dumps calls (sort_keys+separators)
        are explicitly excluded — they hash known-finite dicts for input
        deduplication, not boundary writes to disk."""
        offenders = []
        # Match an ACTUAL call: json.dump( or json.dumps( with the open paren.
        call_re = re.compile(r"\bjson\.dumps?\s*\(")
        for rel_path in PRODUCERS_REQUIRING_ALLOW_NAN_FALSE:
            path = ROOT / rel_path
            src = path.read_text()
            lines = src.splitlines()
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if not call_re.search(stripped):
                    continue
                # Skip non-boundary calls: print/log, comments, asserts.
                if stripped.startswith("#") or stripped.startswith("assert "):
                    continue
                if stripped.startswith("print(") or "print(json.dump" in stripped:
                    continue
                # Hash-cache: sort_keys=True together with separators=. May
                # span the next line; check this line + next as the call window.
                window = "\n".join(lines[i-1:i+2])
                if "sort_keys=True" in window and "separators=" in window:
                    continue
                if "allow_nan=False" not in window:
                    offenders.append(f"{rel_path}:{i}  →  {stripped[:100]}")
        self.assertEqual(offenders, [],
            "R9 P3: every producer boundary json.dump/json.dumps must use "
            "allow_nan=False so NaN/Infinity fails loudly at the producer "
            "boundary instead of silently round-tripping into downstream "
            "consumers. Offenders:\n  " + "\n  ".join(offenders))

    def test_r8_o2_matchday_writer_still_covered(self):
        """Belt-and-braces: the original R8 O2 closure must remain in place."""
        src = (ROOT / "scripts/live/apply_matchday_adjustments.py").read_text()
        # The R8 O2 line:
        assert "allow_nan=False" in src
        # And the audit log writer (R9 P3 addition):
        assert src.count("allow_nan=False") >= 2, (
            "R9 P3: apply_matchday_adjustments.py must carry allow_nan=False "
            "on BOTH the canonical writer (R8 O2) AND the adjustments_log "
            "audit writer (R9 P3 addition)"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
