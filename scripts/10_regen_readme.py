"""
10_regen_readme.py — keep README contender / metrics tables in sync with the
shipped predictions.json + model metrics.

P1-I from round-2 review: the README contender table drifted (24.9% vs the
24.3% in the JSON). Hand-editing the README every refresh is fragile;
instead, regenerate the tables in-place from the source-of-truth files on
every daily-baseline run.

Markers in README.md (kept stable; do not rename):

    <!-- AUTO:TOP_CONTENDERS:BEGIN -->
    ... regenerated table ...
    <!-- AUTO:TOP_CONTENDERS:END -->

    <!-- AUTO:MODEL_METRICS:BEGIN -->
    ... regenerated metrics table ...
    <!-- AUTO:MODEL_METRICS:END -->

Run as part of the nightly job after 03_simulate writes predictions.json:

    python scripts/10_regen_readme.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
MODELS = ROOT / "models"
README = ROOT / "README.md"

BEGIN_C = "<!-- AUTO:TOP_CONTENDERS:BEGIN -->"
END_C = "<!-- AUTO:TOP_CONTENDERS:END -->"
BEGIN_M = "<!-- AUTO:MODEL_METRICS:BEGIN -->"
END_M = "<!-- AUTO:MODEL_METRICS:END -->"


def _snapshot_line(pred: dict) -> str:
    """Freshness banner — the README is a snapshot regenerated nightly by
    this script; live dashboard is always current. Without this line, GitHub
    viewers (and anyone unzipping the repo) saw the table without realising
    how stale it might be."""
    gen = (pred.get("generated_at") or "").split(".")[0].replace("T", " ")
    if gen:
        return (
            f"> _Snapshot: {gen} UTC · regenerates nightly · "
            f"[live dashboard](https://wc26-matchday-intelligence.vercel.app/) "
            f"for current numbers._"
        )
    return (
        "> _Regenerates nightly · "
        "[live dashboard](https://wc26-matchday-intelligence.vercel.app/) "
        "for current numbers._"
    )


def render_contenders_table(pred: dict) -> str:
    teams = pred.get("team_predictions") or []
    n_sims = pred.get("n_simulations_total")
    n_seeds = pred.get("n_seeds")
    n_per = pred.get("n_simulations_per_seed")

    rows = []
    for i, t in enumerate(teams[:6], 1):
        p = t.get("p_champion", 0.0) * 100
        p05 = (t.get("p_champion_p05") or 0.0) * 100
        p95 = (t.get("p_champion_p95") or 0.0) * 100
        sf = (t.get("p_reach_sf") or 0.0) * 100
        elo = int(round(t.get("elo") or 0))
        rows.append(
            f"| {i} | {t['team']:<10} | {p:.1f}% | [{p05:.1f}, {p95:.1f}] | "
            f"{sf:.1f}% | {elo} |"
        )

    header = (
        f"## Top contenders (latest run — "
        f"{n_sims:,} sims, {n_seeds} seeds × {n_per:,})"
        if n_sims else
        "## Top contenders (latest run)"
    )
    return "\n".join([
        header,
        "",
        _snapshot_line(pred),
        "",
        "| # | Team       | Champion | Sim range (5 seeds) | Reach SF | Model Elo |",
        "|---|---|---|---|---|---|",
        *rows,
    ])


def render_metrics_table(pred: dict) -> str:
    metrics_path = MODELS / "metrics_v2.json"
    m = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}
    eval_path = MODELS / "evaluation.json"
    e = json.loads(eval_path.read_text()) if eval_path.exists() else {}
    wf_path = MODELS / "walk_forward.json"
    wf = json.loads(wf_path.read_text()) if wf_path.exists() else {}

    holdout = e.get("holdout") or m.get("holdout") or {}
    ll = holdout.get("log_loss") or holdout.get("logloss")
    br = holdout.get("brier")
    acc = holdout.get("accuracy")

    wf_loglosses = [v.get("log_loss") for v in wf.values()
                    if isinstance(v, dict) and v.get("log_loss") is not None]
    avg_wf = sum(wf_loglosses) / len(wf_loglosses) if wf_loglosses else None

    annex_misses = pred.get("annex_c_misses", 0)

    def cell(v, fmt):
        return fmt.format(v) if v is not None else "—"

    return "\n".join([
        _snapshot_line(pred),
        "",
        "| Metric                          | Value  | Notes |",
        "|---|---|---|",
        f"| Holdout log-loss                | {cell(ll, '{:.3f}')} | lower is better |",
        f"| Holdout Brier                   | {cell(br, '{:.3f}')} | lower is better |",
        f"| Holdout accuracy                | {cell(acc * 100 if acc is not None else None, '{:.1f}%')} | always-home ≈ 48% |",
        f"| WC walk-forward avg log-loss    | {cell(avg_wf, '{:.3f}')} | mean across 2010/14/18/22 |",
        f"| Annex C lookup misses           | {annex_misses}     | target 0 / 25,000+ sims |",
    ])


def _replace_block(text: str, begin: str, end: str, body: str) -> str:
    """Replace the content between begin/end markers. Inserts the markers
    around the first matching legacy header if absent."""
    pattern = re.compile(re.escape(begin) + r"[\s\S]*?" + re.escape(end), re.M)
    block = f"{begin}\n{body}\n{end}"
    if pattern.search(text):
        return pattern.sub(block, text)
    return text  # markers absent — caller is responsible for adding them


def main() -> int:
    pred_path = PROC / "predictions.json"
    if not pred_path.exists():
        print(f"[regen_readme] {pred_path} missing — skipping (no-op).")
        return 0
    pred = json.loads(pred_path.read_text())

    readme = README.read_text()
    new = _replace_block(readme, BEGIN_C, END_C, render_contenders_table(pred))
    new = _replace_block(new, BEGIN_M, END_M, render_metrics_table(pred))
    if new == readme:
        print("[regen_readme] no marker-bounded blocks found (or already up to date).")
        return 0
    README.write_text(new)
    print(f"[regen_readme] updated {README.name} (top contenders + metrics).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
