"""
referee_adjustments.py — Phase 2 referee bias builder.

Reads:
  - data/live/results_2026.json (each match carries a `referee` field
    populated by fetch_results.py from API-Football's `fixture.referee`).
  - data/raw/_proposals/referee_wc2026.json (Wave-A FIFA roster +
    home-win-rate-derived `home_elo_bonus` per referee).

Writes:
  - data/live/referee_2026.json — shape mirrors weather_2026.json so
    apply_matchday_adjustments._load_referee_components() can consume it
    using the same per-side adjustment pattern.

Model (per Wave-A proposal methodology):
  home_elo_bonus = (ref_home_win_rate - 0.58) * 35, clamped to ±REFEREE_CAP.
  Honesty floor: requires n_matches >= MIN_MATCHES_FOR_BONUS, else 0.0.

The bonus is one-sided — it shifts Elo FOR the home team. The away side
always receives 0.0. Modelling it symmetrically would double-count.

Cap (REFEREE_CAP = 8.0) is also enforced by apply_matchday_adjustments
when reading back. Capping at write time keeps the persisted JSON honest.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
RAW = ROOT / "data" / "raw"

REFEREE_CAP = 8.0
MIN_MATCHES_FOR_BONUS = 20  # honesty floor — matches proposal methodology
BASELINE_HOME_WIN_RATE = 0.58
SCALING = 35.0


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2, ensure_ascii=False)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def load_referee_baseline(path: Path | None = None) -> dict:
    """Load the Wave-A referee baseline. Returns the `refs` map keyed by
    canonical referee name. Returns {} if file absent (no-op downstream)."""
    path = path or (RAW / "_proposals" / "referee_wc2026.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data.get("refs", {}) or {}


def _confidence_for(n_matches: int) -> str:
    if n_matches >= 100:
        return "high"
    if n_matches >= MIN_MATCHES_FOR_BONUS:
        return "medium"
    return "none"


def compute_referee_entry(
    match_id: int,
    home_team: str,
    away_team: str,
    referee_name: str | None,
    baseline: dict,
) -> dict:
    """Produce one referee_2026 entry for a single match. Always returns
    a row — zero bonus is still surfaced so the dashboard sees the assignment.
    """
    raw_bonus = 0.0
    n_matches = 0
    notes = None
    confidence = "none"
    reason = "no_referee_assigned"

    if referee_name:
        ref = baseline.get(referee_name)
        if ref is not None:
            n_matches = int(ref.get("n_matches", 0) or 0)
            raw_bonus = float(ref.get("home_elo_bonus", 0.0) or 0.0)
            notes = ref.get("notes")
            confidence = _confidence_for(n_matches)
            # Guard: NaN/inf bonuses survive float() and would otherwise clamp
            # silently to ±REFEREE_CAP with confidence='high'. Neutralise and
            # label so downstream sees the assignment but no Elo movement.
            if not math.isfinite(raw_bonus):
                raw_bonus = 0.0
                confidence = "none"
                reason = "nonfinite_in_baseline"
            elif n_matches < MIN_MATCHES_FOR_BONUS:
                raw_bonus = 0.0
                reason = "honesty_floor_n_matches"
            elif ref.get("nationality") and not ref.get("referee_id"):
                # Soft-silent: baseline is name-keyed but the record carries a
                # nationality field — two referees sharing a name (e.g.
                # "Anthony Taylor" of England vs USA) collide silently on this
                # dict. Without a stable referee_id or caller-supplied
                # nationality cross-check, the record may not match the
                # assigned official. Downgrade reason so consumers can flag.
                reason = "ref_home_bias_name_keyed_only"
            else:
                reason = "ref_home_bias"
        else:
            reason = "ref_not_in_baseline"

    capped = max(-REFEREE_CAP, min(REFEREE_CAP, raw_bonus))
    return {
        "match_id": match_id,
        "home_team": home_team,
        "away_team": away_team,
        "referee_name": referee_name,
        "home_team_adjustment_elo": round(capped, 3),
        "away_team_adjustment_elo": 0.0,
        "raw_home_elo": round(raw_bonus, 3),
        "cap_used": REFEREE_CAP,
        "n_matches": n_matches,
        "confidence": confidence,
        "reason": reason,
        "notes": notes,
    }


def build_referee_2026(
    results_path: Path | None = None,
    baseline_path: Path | None = None,
) -> dict:
    """Build the full referee_2026.json payload from results + baseline."""
    results_path = results_path or (LIVE / "results_2026.json")
    baseline = load_referee_baseline(baseline_path)

    rows = []
    warnings = []
    if results_path.exists():
        try:
            results = json.loads(results_path.read_text(encoding="utf-8"))
        except Exception as e:
            warnings.append({"type": "results_read_error", "message": str(e)})
            results = {}

        for m in results.get("completed_matches", []) or []:
            mid = m.get("m")
            if mid is None:
                continue
            rows.append(compute_referee_entry(
                match_id=int(mid),
                home_team=m.get("home", ""),
                away_team=m.get("away", ""),
                referee_name=m.get("referee"),
                baseline=baseline,
            ))
    else:
        warnings.append({"type": "results_missing",
                         "message": f"{results_path} not present"})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
        "cap_used": REFEREE_CAP,
        "min_matches_for_bonus": MIN_MATCHES_FOR_BONUS,
        "referee": rows,
        "warnings": warnings,
    }


def write_referee_2026_json(
    out_path: Path | None = None,
    results_path: Path | None = None,
    baseline_path: Path | None = None,
) -> Path:
    out_path = out_path or (LIVE / "referee_2026.json")
    payload = build_referee_2026(results_path, baseline_path)
    _atomic_write_json(out_path, payload)
    return out_path


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print payload to stdout, do not write file.")
    args = ap.parse_args(argv)
    payload = build_referee_2026()
    rows = payload["referee"]
    print(f"[referee_adjustments] rows={len(rows)} "
          f"non_zero={sum(1 for r in rows if r['home_team_adjustment_elo'] != 0)}")
    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False)[:2000])
        return 0
    out = write_referee_2026_json()
    print(f"[referee_adjustments] wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
