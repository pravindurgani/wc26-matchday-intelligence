"""
_ko_slot_resolution.py — resolve knockout placeholder slot codes in
schedule rows to real team names, where locked results allow.

KO-phase fix (2026-07-03). `suspension_tracker.load_schedule()` and
`fetch_lineups._load_schedule()` merge `knockout_bracket_2026.json` rows
into the schedule, but those rows carry slot codes ("1A", "W74",
"3A/B/C/D/F") as `home`/`away` until someone resolves them — and nothing
did. Both consumers guard with `is_placeholder_slot` and silently skip,
so for the entire knockout phase:

  * a red card / second yellow in a KO match imposed NO next-match ban
    (`next_match_for_team` skipped every KO row), and
  * no KO fixture ever entered the lineup poll window (KO lineups dark).

This module post-processes schedule rows IN PLACE: any KO row whose
`home`/`away` is a placeholder gets the slot resolved through
`export_ko_advance`'s resolver (`_resolve_group_slots` group ranks +
Annex C third-place routing + W/L winner-loser codes — the same single
source of truth the KO advance-prob export and fetch_results.py's
fixture-map auto-extension use; see fetch_results._resolved_bracket_rows
for the sibling pattern). Slots that cannot be resolved yet keep their
placeholder codes, so the callers' existing skip behavior still fires
for genuinely-unknown fixtures.

No circular import: export_ko_advance imports only tiebreakers /
_knockout / check_invariants / constants — none of which import this
module or its two consumers. The import is nevertheless deferred into
the function and wrapped: ANY resolver failure degrades to unresolved
rows (pre-fix behavior), never a crashed tick.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
LIVE = ROOT / "data" / "live"

# R15 pattern (see fetch_results.py:63 / fetch_lineups.py:74): ROOT on
# sys.path so the absolute `scripts.live.*` import below resolves whether
# the consumer was run as a script or a module.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.live._knockout import is_placeholder_slot  # noqa: E402

DEFAULT_RESULTS = LIVE / "results_2026.json"
DEFAULT_CONFIG = RAW / "wc2026_config.json"
DEFAULT_ANNEX_C = RAW / "annex_c_third_place_table_2026.json"


def resolve_schedule_slots(rows: list[dict],
                           results_path: Path | None = None,
                           config_path: Path | None = None,
                           annex_path: Path | None = None) -> list[dict]:
    """Mutate-and-return `rows`: replace placeholder KO `home`/`away` slot
    codes with real team names wherever locked results permit.

    Only rows tagged with a non-group `stage` (i.e. the rows produced by
    `_knockout.load_knockout_fixtures`) are touched; group rows and
    already-resolved sides pass through untouched. When a side IS
    resolved, the original slot code is preserved on `slot_home` /
    `slot_away` so downstream debugging can still see the bracket wiring.

    Never raises: resolver-import or data failures leave every slot as-is
    and the callers' `is_placeholder_slot` skip paths behave exactly as
    before the fix.
    """
    ko_rows = [
        r for r in rows
        if isinstance(r, dict)
        and r.get("stage") not in (None, "", "group")
        and (is_placeholder_slot(r.get("home"))
             or is_placeholder_slot(r.get("away")))
    ]
    if not ko_rows:
        return rows
    try:
        from scripts.live.export_ko_advance import (  # noqa: PLC0415
            _build_completed_index, _resolve_group_slots, _resolve_slot,
        )
        cfg = json.loads(Path(config_path or DEFAULT_CONFIG).read_text())
        annex_p = Path(annex_path or DEFAULT_ANNEX_C)
        annex_c = json.loads(annex_p.read_text()) if annex_p.exists() else {}
        completed_idx = _build_completed_index(
            Path(results_path or DEFAULT_RESULTS))
        group_slots = _resolve_group_slots(completed_idx, cfg, annex_c)
    except Exception as e:
        print(f"[_ko_slot_resolution] WARN: KO slot resolution unavailable — "
              f"{type(e).__name__}: {e}. Placeholder slots left unresolved.",
              file=sys.stderr)
        return rows
    for row in ko_rows:
        # `r32_match_num` disambiguates third-place fan-out slots
        # ("3A/B/C/D/F") via the Annex C routing keyed on the R32 match
        # number — same ctx shape as export_ko_advance / fetch_results.
        ctx = {"completed_idx": completed_idx, "group_slots": group_slots,
               "r32_match_num": row.get("m")}
        for side in ("home", "away"):
            slot = row.get(side)
            if not is_placeholder_slot(slot):
                continue  # concrete team already written in — keep it
            try:
                resolved = _resolve_slot(slot, ctx)
            except Exception:
                resolved = None
            if resolved and not is_placeholder_slot(resolved):
                row[f"slot_{side}"] = slot
                row[side] = resolved
    return rows
