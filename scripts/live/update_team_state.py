"""
update_team_state.py — Soft mid-tournament team strength updates.

We do NOT retrain the model. We apply a *capped* Elo delta after each completed
match using:
  - opponent strength
  - actual goal difference
  - match importance (locked tournament match = K=60)
  - margin multiplier

Caps:
  - max ±12 Elo per single match
  - max ±30 Elo total movement per team across group stage
  - max ±15 Elo per knockout match

Writes data/live/live_team_state.json with per-team {delta_elo, last_update}.
The simulator (--live) reads this and applies as an Elo bump before lambdas.
"""
from __future__ import annotations
import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
RAW = ROOT / "data" / "raw"
PROC = ROOT / "data" / "processed"


def _atomic_write_json(path: Path, payload: dict) -> None:
    """R11 D5: atomic tempfile + os.replace. Pre-R11 this file used bare
    .write_text(json.dumps(...)) — SIGKILL / OOM / disk-full mid-write
    leaves a partial JSON on disk that the simulator parses with bare
    json.loads at 03_simulate.py:698, raising mid-load and crashing the
    tick. Mirrors the pattern in run_live_update.atomic_write_json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2, allow_nan=False, default=str)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

K_TOURNAMENT = 60
MAX_PER_MATCH = 12.0
MAX_PER_TEAM_GROUP = 30.0
MAX_PER_KNOCKOUT = 15.0


def margin_multiplier(gd: int) -> float:
    g = abs(gd)
    if g <= 1: return 1.0
    if g == 2: return 1.5
    if g == 3: return 1.75
    return 1.75 + (g - 3) / 8.0


def expected_score(rh: float, ra: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((ra - rh) / 400.0))


def main():
    results_path = LIVE / "results_2026.json"
    if not results_path.exists():
        print("[update_team_state] no results_2026.json — nothing to do")
        return

    elo = json.loads((PROC / "elo_ratings.json").read_text())
    results = json.loads(results_path.read_text())
    cfg = json.loads((RAW / "wc2026_config.json").read_text())
    schedule_by_m = {f["m"]: f for f in cfg["group_stage_schedule"]}

    completed = results.get("completed_matches", [])
    if not completed:
        out = {"schema": "soft Elo delta per team since tournament start",
               "deltas": {}, "n_processed": 0,
               # R11 D5: last_updated read by compute_input_hash
               # (run_live_update.py:278, 03_simulate.py:1171) — pre-R11
               # the field was missing → hash always saw empty string →
               # a stalled writer that re-emitted identical deltas
               # forever was invisible to the hash gate.
               "last_updated": _now_iso()}
        _atomic_write_json(LIVE / "live_team_state.json", out)
        print("[update_team_state] no completed matches yet")
        return

    deltas: dict[str, float] = defaultdict(float)
    deltas_count: dict[str, int] = defaultdict(int)
    processed = 0
    for m in completed:
        fx = schedule_by_m.get(m["m"])
        if not fx:
            continue
        h, a = fx["home"], fx["away"]
        # Use updated effective Elo (base + accumulated delta so far)
        rh = elo.get(h, 1500) + deltas[h]
        ra = elo.get(a, 1500) + deltas[a]
        gd = m["home_score"] - m["away_score"]
        eh = expected_score(rh, ra)
        if gd > 0:    sh, sa = 1.0, 0.0
        elif gd < 0:  sh, sa = 0.0, 1.0
        else:         sh, sa = 0.5, 0.5
        mm = margin_multiplier(gd)
        d_h = K_TOURNAMENT * mm * (sh - eh)
        d_a = K_TOURNAMENT * mm * (sa - (1 - eh))
        # Cap per-match swing
        d_h = max(-MAX_PER_MATCH, min(MAX_PER_MATCH, d_h))
        d_a = max(-MAX_PER_MATCH, min(MAX_PER_MATCH, d_a))
        deltas[h] += d_h
        deltas[a] += d_a
        deltas_count[h] += 1
        deltas_count[a] += 1
        processed += 1

    # Cap aggregate per-team movement
    capped = {}
    for team, d in deltas.items():
        cap = MAX_PER_TEAM_GROUP if deltas_count[team] <= 3 else MAX_PER_TEAM_GROUP + MAX_PER_KNOCKOUT
        capped[team] = float(max(-cap, min(cap, d)))

    out = {
        "schema": "soft Elo delta per team since tournament start",
        "policy": {"K_tournament": K_TOURNAMENT,
                   "max_per_match": MAX_PER_MATCH,
                   "max_total_group": MAX_PER_TEAM_GROUP,
                   "max_per_knockout": MAX_PER_KNOCKOUT},
        "n_processed": processed,
        "deltas": capped,
        # R11 D5: timestamp for compute_input_hash freshness signal.
        "last_updated": _now_iso(),
    }
    _atomic_write_json(LIVE / "live_team_state.json", out)
    print(f"[update_team_state] processed {processed} matches, {len(capped)} teams adjusted")
    for t, d in sorted(capped.items(), key=lambda kv: -abs(kv[1]))[:5]:
        print(f"  {t:<25s} {d:+.1f} Elo")


if __name__ == "__main__":
    main()
