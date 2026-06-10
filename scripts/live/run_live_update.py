"""
run_live_update.py — Live update orchestrator.

Idempotent. Safe to run every 10-15 minutes during the tournament.

Flow:
  1. Fetch results (fetch_results.py) — populates results_2026.json
  2. Diff vs previous run — exit early if no new FT matches
  3. Re-compute soft team-state Elo deltas (update_team_state.py)
  4. Re-run live simulation (03_simulate.py --live)
  5. Build live_delta.json (predictions_static vs predictions_live)
  6. Write live_state.json with mode, last_updated, etc.
  7. Copy artifacts to dashboard/
  8. Run validator

Hardening (Jun 2026):
  - Atomic writes for live_state.json and live_delta.json
  - Sim failure preserves the previous predictions_live.json (no corrupt overwrite)
  - Postponed/abandoned matches surface as warnings on live_state
  - Circuit breaker: 3 consecutive sim failures backs off and writes
    {"mode": "live", "warning": "..."} until a human intervenes
  - Top-level try/except so a partial crash still produces a usable live_state
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "data" / "live"
PROC = ROOT / "data" / "processed"
MODELS = ROOT / "models"
DASH = ROOT / "dashboard"
CB_PATH = LIVE / "circuit_breaker_state.json"
CB_THRESHOLD = 3  # consecutive sim failures before tripping the breaker

# C1: required artifacts for --live sim. Missing any of these crashes the sim
# silently behind the circuit breaker; we fail loud BEFORE invoking 03_simulate.
REQUIRED_ARTIFACTS = [
    MODELS / "home_goals_model.joblib",
    MODELS / "away_goals_model.joblib",
    PROC / "matches_clean.parquet",
    MODELS / "feature_cols_v2.json",
    MODELS / "metrics_v2.json",
    PROC / "elo_ratings.json",
]


def check_required_artifacts() -> list[Path]:
    """Returns the list of missing required artifacts (empty if all present)."""
    return [p for p in REQUIRED_ARTIFACTS if not p.exists()]


def run(cmd: list[str]) -> int:
    print(f"  → {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def atomic_write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def read_circuit_breaker() -> int:
    if not CB_PATH.exists():
        return 0
    try:
        return int(json.loads(CB_PATH.read_text()).get("consecutive_failures", 0))
    except Exception:
        return 0


def write_circuit_breaker(failures: int):
    atomic_write_json(CB_PATH, {
        "consecutive_failures": failures,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "threshold": CB_THRESHOLD,
    })


def get_completed_count() -> int:
    p = LIVE / "results_2026.json"
    if not p.exists():
        return 0
    try:
        return len(json.loads(p.read_text()).get("completed_matches", []))
    except Exception:
        return 0


def get_results_warnings() -> list:
    p = LIVE / "results_2026.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text()).get("warnings", []) or []
    except Exception:
        return []


def get_in_play_matches() -> list:
    """P1-G: read in_play list from results_2026.json so the dashboard can
    render a 'LIVE now' strip during matches without waiting for FT."""
    p = LIVE / "results_2026.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text()).get("in_play", []) or []
    except Exception:
        return []


def get_live_predictions_locked_count() -> int:
    """How many matches were locked in the most recent predictions_live.json."""
    p = PROC / "predictions_live.json"
    if not p.exists():
        return -1
    try:
        return len(json.loads(p.read_text()).get("completed_matches", []))
    except Exception:
        return -1


def compute_input_hash() -> str:
    """H1: SHA-256 over (results.completed_matches + matchday_intelligence.generated_at
    + live_team_state.last_updated). Detects score corrections and intel refreshes
    that the bare match-count check would miss.
    """
    h = hashlib.sha256()
    # Completed matches (sorted by match number → stable)
    res = LIVE / "results_2026.json"
    if res.exists():
        try:
            data = json.loads(res.read_text())
            cm = sorted(data.get("completed_matches", []), key=lambda m: m.get("m", 0))
            h.update(json.dumps(cm, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        except Exception:
            h.update(b"results_unreadable")
    # Matchday intelligence freshness (any layer change)
    mi = DASH / "matchday_intelligence.json"
    if mi.exists():
        try:
            data = json.loads(mi.read_text())
            h.update(str(data.get("generated_at", "")).encode("utf-8"))
            # Also hash the aggregated counts so a fetcher silently emptying
            # a layer still bumps the hash.
            adj = data.get("adjustments", []) or []
            h.update(str(len(adj)).encode("utf-8"))
        except Exception:
            h.update(b"mi_unreadable")
    # Live team state. Schema is {"deltas": {team: float}, "last_updated":...}
    lts = LIVE / "live_team_state.json"
    if lts.exists():
        try:
            data = json.loads(lts.read_text())
            h.update(str(data.get("last_updated", "")).encode("utf-8"))
            deltas = data.get("deltas", {}) or {}
            h.update(json.dumps(deltas, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        except Exception:
            h.update(b"lts_unreadable")
    return h.hexdigest()[:16]


def read_last_input_hash() -> str:
    """Read the hash stored in predictions_live.json on the previous tick."""
    p = PROC / "predictions_live.json"
    if not p.exists():
        return ""
    try:
        return str(json.loads(p.read_text()).get("input_hash", ""))
    except Exception:
        return ""


def detect_provider_source() -> tuple[str, str]:
    """Returns (source_label, provider_mode).

    source_label: human-readable string surfaced on the dashboard
    provider_mode: 'active' if a real provider key is configured, else 'manual'
    """
    provider = (os.environ.get("FOOTBALL_PROVIDER")
                or os.environ.get("WC_RESULTS_SOURCE")
                or "mock").strip().lower().replace("-", "_")
    apifootball_key = (os.environ.get("API_FOOTBALL_KEY")
                       or os.environ.get("WC_APIFOOTBALL_KEY"))
    football_data_token = (os.environ.get("FOOTBALL_DATA_TOKEN")
                           or os.environ.get("WC_FOOTBALL_DATA_TOKEN"))
    sportmonks_token = (os.environ.get("SPORTMONKS_TOKEN")
                        or os.environ.get("WC_SPORTMONKS_TOKEN"))
    if provider in ("api_football", "apifootball") and apifootball_key:
        return "api_football", "active"
    if provider in ("football_data", "footballdata") and football_data_token:
        return "football_data", "active"
    if provider == "sportmonks" and sportmonks_token:
        return "sportmonks", "active"
    return "manual/mock", "manual"


def write_live_state(mode: str, completed_count: int, sim_rerun: bool,
                     warnings: list | None = None, source: str | None = None,
                     provider_mode: str | None = None,
                     in_play: list | None = None):
    """Atomic live_state.json write."""
    if source is None or provider_mode is None:
        auto_source, auto_mode = detect_provider_source()
        source = source or auto_source
        provider_mode = provider_mode or auto_mode
    if in_play is None:
        in_play = get_in_play_matches()
    state = {
        "mode": mode,
        "last_updated_utc": datetime.now(timezone.utc).isoformat(),
        "completed_matches_count": completed_count,
        "simulation_rerun_this_tick": sim_rerun,
        "source": source,
        "provider_mode": provider_mode,
        "in_play": in_play,            # P1-G: dashboard LIVE strip
        "in_play_count": len(in_play), # convenient summary
        "warnings": warnings or [],
    }
    # Deploy-churn guard: if every field except last_updated_utc is identical
    # to what's already on disk, preserve the old timestamp so the file's
    # bytes don't change. The workflow's git-add then sees no diff and skips
    # the commit, which keeps Vercel Hobby's 100 deploys/day cap comfortable
    # during quiet ticks and during in-play windows where nothing has moved.
    try:
        existing = json.loads((DASH / "live_state.json").read_text())
        a = {k: v for k, v in state.items() if k != "last_updated_utc"}
        b = {k: v for k, v in existing.items() if k != "last_updated_utc"}
        if a == b and existing.get("last_updated_utc"):
            state["last_updated_utc"] = existing["last_updated_utc"]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    atomic_write_json(DASH / "live_state.json", state)
    return state


def write_empty_delta():
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "top_movers_up": [], "top_movers_down": [], "all_movers": [],
    }
    atomic_write_json(DASH / "live_delta.json", out)
    return out


def build_live_delta(min_pp: float = 0.5):
    """Diff predictions_static.json vs predictions_live.json → live_delta.json.

    `min_pp` filters out movers below the seed-noise threshold (~0.3-0.5pp
    for 5×5000 vs 3×3000 sample sizes). Pre-tournament deltas should be
    written via write_empty_delta() instead.
    """
    static_p = PROC / "predictions.json"
    live_p = PROC / "predictions_live.json"
    if not static_p.exists() or not live_p.exists():
        return None
    try:
        s = json.loads(static_p.read_text())
        l = json.loads(live_p.read_text())
    except Exception as e:
        print(f"[run_live_update] could not parse predictions for delta: {e}")
        return None
    static_p_by_t = {t["team"]: t["p_champion"] for t in s.get("team_predictions", [])}
    live_p_by_t = {t["team"]: t["p_champion"] for t in l.get("team_predictions", [])}
    movers = []
    for team, lp in live_p_by_t.items():
        sp = static_p_by_t.get(team, 0)
        delta_pp = (lp - sp) * 100
        if abs(delta_pp) < min_pp:
            continue
        movers.append({"team": team, "static": sp, "live": lp, "delta_pp": delta_pp})
    movers.sort(key=lambda d: -abs(d["delta_pp"]))
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_pp_threshold": min_pp,
        "top_movers_up":   [m for m in movers if m["delta_pp"] > 0][:10],
        "top_movers_down": [m for m in movers if m["delta_pp"] < 0][:10],
        "all_movers": movers,
    }
    atomic_write_json(DASH / "live_delta.json", out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None,
                    help="Override provider (mock | api_football | sportmonks). "
                         "Default: FOOTBALL_PROVIDER env, then WC_RESULTS_SOURCE, then mock.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch + diff but do not re-simulate or write dashboard JSON.")
    args = ap.parse_args()

    if args.provider:
        os.environ["FOOTBALL_PROVIDER"] = args.provider

    print("== Live update tick ==" + (" [dry-run]" if args.dry_run else ""))

    failures = read_circuit_breaker()
    if failures >= CB_THRESHOLD:
        msg = f"Circuit breaker tripped after {failures} consecutive failures. " \
              f"Manual intervention required: reset by deleting {CB_PATH}."
        print(f"[run_live_update] {msg}")
        # Still emit live_state so the dashboard reflects the situation
        write_live_state("live", get_completed_count(), sim_rerun=False,
                         warnings=[{"type": "circuit_breaker", "message": msg}])
        return 2

    # Step 1: fetch results (pass --dry-run through)
    fetch_cmd = [sys.executable, "scripts/live/fetch_results.py"]
    if args.dry_run:
        fetch_cmd.append("--dry-run")
    rc = run(fetch_cmd)
    if rc != 0:
        print("[run_live_update] fetch_results failed — emitting warning, keeping prior state")
        write_live_state("live" if get_completed_count() > 0 else "pre_tournament",
                         get_completed_count(), sim_rerun=False,
                         warnings=[{"type": "fetch_failure",
                                    "message": "Live result fetcher exited non-zero; "
                                               "previous predictions retained."}])
        return 0  # don't trip CB for fetch failure — that's transient

    new_count = get_completed_count()
    warns = get_results_warnings()
    last_synced = get_live_predictions_locked_count()

    # H1: hash-based change detection. Catches score corrections AND
    # matchday-intel updates that the bare count check misses.
    current_hash = compute_input_hash()
    last_hash = read_last_input_hash()
    inputs_changed = (current_hash != last_hash) or (last_synced != new_count)

    # Step 2: early exit if NOTHING upstream has moved since the last sim
    if not inputs_changed and last_synced >= 0:
        print(f"[run_live_update] inputs unchanged (count={new_count}, hash={current_hash}) — skipping sim")
        mode = "pre_tournament" if new_count == 0 else "live"
        write_live_state(mode, new_count, sim_rerun=False, warnings=warns)
        write_circuit_breaker(0)  # success path resets
        return 0

    if args.dry_run:
        print(f"[run_live_update] dry-run: would re-simulate "
              f"({last_synced} locked → {new_count} locked, hash {last_hash!r} → {current_hash!r})")
        mode = "pre_tournament" if new_count == 0 else "live"
        write_live_state(mode, new_count, sim_rerun=False, warnings=warns)
        return 0

    # C1: fail loud if the artifacts the sim needs are absent. Without this
    # check, 03_simulate raises FileNotFoundError, the circuit breaker burns
    # three ticks, and the dashboard freezes at pre-tournament numbers for
    # the rest of the tournament.
    missing = check_required_artifacts()
    if missing:
        missing_names = [str(p.relative_to(ROOT)) for p in missing]
        msg = ("Required artifacts missing — sim cannot run: "
               + ", ".join(missing_names)
               + ". Re-run daily-baseline or train locally.")
        print(f"[run_live_update] {msg}")
        write_live_state("live" if new_count > 0 else "pre_tournament",
                         new_count, sim_rerun=False,
                         warnings=warns + [{
                             "type": "missing_model_artifacts",
                             "message": msg,
                             "missing": missing_names,
                         }])
        # Don't trip the circuit breaker for a setup problem — that needs a human.
        return 2

    # Step 3: update team state (soft Elo deltas) — non-fatal if it fails
    rc = run([sys.executable, "scripts/live/update_team_state.py"])
    if rc != 0:
        print("[run_live_update] update_team_state failed; continuing without it")

    # Step 4: re-run live simulation
    print(f"[run_live_update] {new_count} matches completed, re-simulating…")
    rc = run([sys.executable, "scripts/03_simulate.py",
              "--live", "--seeds", "3", "--sims", "3000",
              "--out", "predictions_live.json"])
    if rc != 0:
        new_failures = failures + 1
        write_circuit_breaker(new_failures)
        write_live_state("live" if new_count > 0 else "pre_tournament",
                         new_count, sim_rerun=False,
                         warnings=warns + [{
                             "type": "sim_failure",
                             "message": f"Live simulation failed ({new_failures}/{CB_THRESHOLD}); "
                                        "previous predictions_live.json retained.",
                         }])
        return 1

    # Success: reset breaker
    write_circuit_breaker(0)

    # Step 5: live delta — only meaningful once matches are locked
    if new_count > 0:
        delta = build_live_delta()
    else:
        delta = write_empty_delta()

    # Step 6: live state
    mode = "live" if new_count > 0 else "pre_tournament"
    write_live_state(mode, new_count, sim_rerun=True, warnings=warns)

    # Step 7: copy to dashboard (atomic via rename)
    src = PROC / "predictions_live.json"
    if src.exists():
        dst = DASH / "predictions_live.json"
        try:
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            tmp.write_bytes(src.read_bytes())
            os.replace(tmp, dst)
        except Exception as e:
            print(f"[run_live_update] failed to copy predictions_live.json to dashboard: {e}")

    # Step 8: validator
    run([sys.executable, "scripts/09_validate.py"])

    print(f"[run_live_update] DONE — locked {new_count} matches")
    if delta and delta.get("top_movers_up"):
        top = delta["top_movers_up"][0]
        print(f"  Top mover: {top['team']} ({top['delta_pp']:+.2f}pp)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[run_live_update] FATAL {type(e).__name__}: {e}")
        traceback.print_exc()
        # Best-effort: write a warning to live_state so the dashboard knows
        try:
            write_live_state("live" if get_completed_count() > 0 else "pre_tournament",
                             get_completed_count(), sim_rerun=False,
                             warnings=[{"type": "orchestrator_crash",
                                        "message": f"{type(e).__name__}: {e}"[:200]}])
        except Exception:
            pass
        sys.exit(1)
