"""
auto_tier_diff.py — Phase 6 disagreement-diff CLI.

Compare the auto-derived tier (auto_tier.auto_classify) against the
hand-curated whitelist tier (data/raw/key_players_2026.json) for every
player we have stats for. Emits a markdown table of disagreements with
the driving components (minutes_share, ga90, cs_share) so the operator
can review before flipping `auto_tier_active: true`.

CLI:
  python3 scripts/live/auto_tier_diff.py
  python3 scripts/live/auto_tier_diff.py --stats data/live/player_stats_2026.json
  python3 scripts/live/auto_tier_diff.py --fixture tests/.../fixture.json
  python3 scripts/live/auto_tier_diff.py --out reports/auto_tier_diff.md

Exit codes:
  0  ran cleanly (any number of disagreements is informational)
  1  could not load stats payload (missing file, malformed JSON)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from auto_tier import auto_classify  # noqa: E402
from fetch_player_stats import to_stats  # noqa: E402
from injury_adjustments import KEY_PLAYERS_PATH  # noqa: E402


def _load_whitelist(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return list(data.get("players") or [])


def _load_stats(path: Path | None) -> dict:
    if path is None:
        path = ROOT / "data" / "live" / "player_stats_2026.json"
    if not path.exists():
        raise FileNotFoundError(f"stats file not found: {path}")
    return json.loads(path.read_text())


def build_disagreements(whitelist: list[dict], stats_snap: dict) -> list[dict]:
    teams = stats_snap.get("teams") or {}
    rows: list[dict] = []
    for entry in whitelist:
        team = entry.get("team")
        name = entry.get("name")
        override_tier = entry.get("tier")
        if not team or not name:
            continue
        team_payload = teams.get(team) or {}
        stats = to_stats(team_payload, name)
        auto_tier, auto_source, comp = auto_classify(stats)
        if auto_tier == override_tier:
            continue
        # Small-sample guard: auto_tier is explicitly *not making a
        # call* for these teams, so they cannot be "disagreements".
        # The priority chain falls through to override / DEFAULT_TIER
        # at runtime (auto_tier=None → no auto signal).
        if auto_source == "auto_insufficient_sample":
            continue
        rows.append({
            "team": team,
            "player": name,
            "override_tier": override_tier,
            "auto_tier": auto_tier,
            "auto_source": auto_source,
            "minutes_share": comp.get("minutes_share"),
            "ga90": comp.get("ga90"),
            "cs_share": comp.get("cs_share"),
            "minutes": comp.get("minutes"),
            "team_top_minutes": comp.get("team_top_minutes"),
        })
    rows.sort(key=lambda r: (r["team"], r["player"]))
    return rows


def render_markdown(rows: list[dict], header_meta: dict) -> str:
    lines = [
        "# Auto-tier disagreement diff",
        "",
        f"- whitelist entries reviewed: **{header_meta['whitelist_count']}**",
        f"- stats teams available: **{header_meta['stats_teams']}**",
        f"- disagreements: **{len(rows)}**",
        f"- stats generated_at: `{header_meta.get('stats_generated_at', 'unknown')}`",
        "",
    ]
    if not rows:
        lines.append("_No disagreements — auto_tier matches the override layer "
                     "for every player with stats data._")
        return "\n".join(lines) + "\n"
    lines.extend([
        "| Team | Player | Override | Auto | Source | min_share | ga/90 | cs_share | minutes |",
        "|------|--------|----------|------|--------|-----------|-------|----------|---------|",
    ])
    for r in rows:
        lines.append(
            f"| {r['team']} | {r['player']} | {r['override_tier']} | "
            f"{r['auto_tier']} | {r['auto_source']} | "
            f"{r['minutes_share']} | {r['ga90']} | {r['cs_share']} | "
            f"{r['minutes']}/{r['team_top_minutes']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Diff auto_tier vs hand-curated key_players_2026.json.")
    ap.add_argument("--stats", type=Path, default=None,
                    help="Path to player_stats_2026.json (default: data/live/...).")
    ap.add_argument("--fixture", type=Path, default=None,
                    help="Alias for --stats; for symmetry with fetch_player_stats.")
    ap.add_argument("--whitelist", type=Path, default=KEY_PLAYERS_PATH,
                    help="Hand-curated whitelist (default: data/raw/key_players_2026.json).")
    ap.add_argument("--out", type=Path, default=None,
                    help="Write markdown to this file instead of stdout.")
    args = ap.parse_args()

    stats_path = args.fixture or args.stats
    try:
        stats_snap = _load_stats(stats_path)
    except FileNotFoundError as exc:
        print(f"[auto_tier_diff] {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"[auto_tier_diff] malformed stats JSON: {exc}", file=sys.stderr)
        return 1

    whitelist = _load_whitelist(args.whitelist)
    rows = build_disagreements(whitelist, stats_snap)
    header_meta = {
        "whitelist_count": len(whitelist),
        "stats_teams": len(stats_snap.get("teams") or {}),
        "stats_generated_at": stats_snap.get("generated_at"),
    }
    md = render_markdown(rows, header_meta)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md)
        print(f"[auto_tier_diff] wrote {args.out} ({len(rows)} disagreements)")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
