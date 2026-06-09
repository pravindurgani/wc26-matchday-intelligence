"""
Empirical probe of API-Football's knockout-stage response shape.

Why this exists
---------------
Before extending the fixture-map builder and the live fetcher to handle
WC2026 knockouts (M73-M104) — including penalty shootouts and pre-draw
placeholder fixtures — we need ground-truth on the EXACT field shape
API-Football returns for completed knockout tournaments. Guessing leads
to silent drops on 28 June when R32 kicks off and there's no fallback.

This script:
  1. Hits `/fixtures?league={4|1}&season={2024|2022}` for Euro 2024 and
     World Cup 2022 (both completed, fully populated, similar shape to
     what WC2026 will look like once the bracket resolves).
  2. Filters to knockout rounds only (R16 / QF / SF / Final).
  3. Strips noise (photos, full lineup blocks, advertising fields) and
     keeps the fields we genuinely need to ground the spec:
       fixture.id, fixture.status.{short,long}
       league.round
       teams.{home,away}.{id,name,winner}
       goals.{home,away}
       score.fulltime, score.extratime, score.penalty
  4. Writes one sanitized JSON file per tournament under
     tests/live/provider_samples/.
  5. Prints a human-readable summary table (and a markdown summary file)
     of the observed status codes, round labels, and which fixtures went
     to AET vs PEN — the artefacts that lock the schema for A.1-A.3.

CI-only execution
-----------------
This script MUST be invoked through the dedicated workflow
.github/workflows/probe-apifootball.yml because:
  * It needs API_FOOTBALL_KEY, which lives only in GitHub secrets
  * The key is never printed (we don't pass it as a CLI arg or log it)
  * GitHub redacts secret values from logs even if accidentally echoed

Local invocation is intentionally NOT supported — if you really need to
run it locally for development, export API_FOOTBALL_KEY temporarily and
accept the risk that shell history may capture it.

Idempotent: re-running overwrites the sample files with the latest API
shape. Hits exactly 2 requests (well under the 7500/day Pro quota).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SAMPLES_DIR = ROOT / "tests" / "live" / "provider_samples"
SUMMARY_PATH = SAMPLES_DIR / "apifootball_probe_summary.md"

APIFOOTBALL_BASE = "https://v3.football.api-sports.io"

# Tournaments to probe. Both completed, both knockout-rich.
TARGETS = [
    {
        "label": "Euro 2024",
        "league_id": 4,
        "season": 2024,
        "out_path": SAMPLES_DIR / "apifootball_euro2024_knockouts.json",
    },
    {
        "label": "World Cup 2022",
        "league_id": 1,
        "season": 2022,
        "out_path": SAMPLES_DIR / "apifootball_wc2022_knockouts.json",
    },
]

# Status codes we care about for knockout decisioning.
LOCKED_STATUSES = {"FT", "AET", "PEN"}

# Round labels we want to keep (drops group-stage matches without
# requiring us to know how API-Football names group rounds).
# This list mirrors what API-Football historically returns; the probe
# itself will reveal any drift.
KNOCKOUT_ROUND_HINTS = (
    "round of",     # "Round of 16"  / "Round of 32"
    "1/8",          # "1/8-finals"   (some leagues)
    "1/16",
    "quarter",      # "Quarter-finals"
    "semi",         # "Semi-finals"
    "final",        # "Final" / "3rd Place Final"
    "3rd place",
)


def get_key() -> str:
    """Read the API-Football key from env. Never log its value."""
    key = (
        os.environ.get("API_FOOTBALL_KEY")
        or os.environ.get("WC_APIFOOTBALL_KEY")
    )
    if not key:
        print(
            "[probe] FATAL: API_FOOTBALL_KEY not set in environment.\n"
            "        This script is designed to run in CI only — see\n"
            "        .github/workflows/probe-apifootball.yml.",
            file=sys.stderr,
        )
        sys.exit(2)
    return key


def http_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    """Single GET, no retry — probe is one-shot so a failure is loud
    and immediate rather than masked behind backoff."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        print(f"[probe] HTTP {e.code} from {url}: {e.read()[:200]!r}", file=sys.stderr)
        raise
    except urllib.error.URLError as e:
        print(f"[probe] URL error from {url}: {e}", file=sys.stderr)
        raise


def is_knockout_round(round_label: str | None) -> bool:
    if not round_label:
        return False
    label_lower = round_label.lower()
    return any(hint in label_lower for hint in KNOCKOUT_ROUND_HINTS)


def sanitize_fixture(f: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields that ground the schema decisions for A.1-A.3.

    Specifically drops:
      * fixture.referee, periods, venue.city (not needed for the probe)
      * teams.*.logo (URL, noise)
      * league.flag, league.logo, league.country (we only need round + season)
    Keeps:
      * fixture.id, status (short, long, elapsed)
      * league.round
      * teams.{home,away}.{id, name, winner}
      * goals.{home, away}
      * score.fulltime, score.extratime, score.penalty
    """
    fixture = f.get("fixture") or {}
    status = fixture.get("status") or {}
    league = f.get("league") or {}
    teams = f.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    goals = f.get("goals") or {}
    score = f.get("score") or {}

    return {
        "fixture": {
            "id": fixture.get("id"),
            "date": fixture.get("date"),
            "status": {
                "short": status.get("short"),
                "long": status.get("long"),
                "elapsed": status.get("elapsed"),
            },
        },
        "league": {
            "id": league.get("id"),
            "season": league.get("season"),
            "round": league.get("round"),
        },
        "teams": {
            "home": {
                "id": home.get("id"),
                "name": home.get("name"),
                "winner": home.get("winner"),  # true/false/None
            },
            "away": {
                "id": away.get("id"),
                "name": away.get("name"),
                "winner": away.get("winner"),
            },
        },
        "goals": {
            "home": goals.get("home"),
            "away": goals.get("away"),
        },
        "score": {
            "halftime": score.get("halftime"),
            "fulltime": score.get("fulltime"),
            "extratime": score.get("extratime"),
            "penalty": score.get("penalty"),
        },
    }


def probe_one(target: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """Fetch one tournament, filter to knockouts, sanitize, write."""
    url = (
        f"{APIFOOTBALL_BASE}/fixtures"
        f"?league={target['league_id']}&season={target['season']}"
    )
    print(f"[probe] GET {url}  (label={target['label']})")
    data = http_get_json(url, headers=headers)

    raw_fixtures = data.get("response") or []
    if not raw_fixtures:
        # API-Football wraps errors in {errors: {...}}; surface them.
        err = data.get("errors") or {}
        print(f"[probe] No fixtures returned. errors={err!r}", file=sys.stderr)
        return {"label": target["label"], "fixtures": [], "error": err}

    knockouts = [
        sanitize_fixture(f)
        for f in raw_fixtures
        if is_knockout_round((f.get("league") or {}).get("round"))
    ]

    payload = {
        "_probe_metadata": {
            "tournament": target["label"],
            "league_id": target["league_id"],
            "season": target["season"],
            "total_fixtures_returned": len(raw_fixtures),
            "knockout_fixtures_kept": len(knockouts),
            "probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "note": (
                "Sanitized via scripts/research/probe_apifootball_knockouts.py. "
                "Only fields needed to ground the A.1-A.3 schema were kept."
            ),
        },
        "fixtures": knockouts,
    }

    target["out_path"].parent.mkdir(parents=True, exist_ok=True)
    target["out_path"].write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(
        f"[probe]   wrote {target['out_path'].relative_to(ROOT)} "
        f"({len(knockouts)} knockout fixtures)"
    )
    return payload


def summarize(payloads: list[dict[str, Any]]) -> str:
    """Build a human-readable markdown summary of what the probe found.

    The summary is the *real* output of A.0 — it's what A.1-A.3 will
    reference when deciding how to parse placeholder team names, PEN
    sub-scores, and the winner flag.
    """
    lines = [
        "# API-Football knockout-shape probe — summary",
        "",
        "Generated by `scripts/research/probe_apifootball_knockouts.py` "
        "as part of Stream A (knockout fixture map + PEN scores). This file "
        "is the empirical ground-truth that A.1-A.3 are built against.",
        "",
        "## Tournaments probed",
        "",
    ]

    for p in payloads:
        meta = p.get("_probe_metadata", {})
        fixtures = p.get("fixtures", [])
        lines += [
            f"### {meta.get('tournament', '?')}",
            f"- League ID: `{meta.get('league_id')}`, Season: `{meta.get('season')}`",
            f"- Total fixtures returned: {meta.get('total_fixtures_returned', 0)}",
            f"- Knockout fixtures kept: {meta.get('knockout_fixtures_kept', 0)}",
            f"- Probed at (UTC): `{meta.get('probed_at_utc', '?')}`",
            "",
        ]

        if not fixtures:
            lines += ["_No knockout fixtures captured._", ""]
            continue

        # Distinct round labels observed.
        round_labels = sorted({f["league"]["round"] for f in fixtures if f["league"].get("round")})
        lines += [
            "**Distinct round labels observed:**",
            "",
            *[f"- `{r}`" for r in round_labels],
            "",
        ]

        # Distinct status codes observed (FT/AET/PEN/NS/...).
        status_codes = sorted({f["fixture"]["status"]["short"] for f in fixtures if f["fixture"]["status"].get("short")})
        lines += [
            "**Distinct status codes observed:**",
            "",
            *[f"- `{s}`" for s in status_codes],
            "",
        ]

        # Show the first AET fixture and the first PEN fixture in detail —
        # those are the two schemas A.2 must parse.
        aet_examples = [f for f in fixtures if f["fixture"]["status"]["short"] == "AET"]
        pen_examples = [f for f in fixtures if f["fixture"]["status"]["short"] == "PEN"]

        if aet_examples:
            ex = aet_examples[0]
            lines += [
                "**Sample AET fixture (extra-time decided, no shootout):**",
                "",
                "```json",
                json.dumps(ex, indent=2, ensure_ascii=False),
                "```",
                "",
            ]
        if pen_examples:
            ex = pen_examples[0]
            lines += [
                "**Sample PEN fixture (penalty shootout):**",
                "",
                "```json",
                json.dumps(ex, indent=2, ensure_ascii=False),
                "```",
                "",
            ]

        # Any pre-draw placeholder fixtures (status NS / TBD with placeholder names)?
        # Useful for understanding what WC2026 R32 looks like BEFORE group stage ends.
        placeholders = [
            f for f in fixtures
            if f["fixture"]["status"].get("short") in {"NS", "TBD", "PST"}
            or (f["teams"]["home"].get("name") or "").lower().startswith(("winner ", "runner ", "loser ", "3rd "))
            or (f["teams"]["away"].get("name") or "").lower().startswith(("winner ", "runner ", "loser ", "3rd "))
        ]
        if placeholders:
            ex = placeholders[0]
            lines += [
                f"**Sample placeholder fixture (pre-draw):** {len(placeholders)} found",
                "",
                "```json",
                json.dumps(ex, indent=2, ensure_ascii=False),
                "```",
                "",
            ]
        else:
            lines += [
                "_No placeholder fixtures found — tournament has fully resolved._",
                "",
                "_For WC2026 placeholder semantics, we'll need to probe again _"
                "_during the actual tournament (or accept the inference that_"
                "_API-Football uses similar wording to past competitions)._",
                "",
            ]

    lines += [
        "## What this proves for A.1 / A.2 / A.3",
        "",
        "1. **A.1 (builder)** — round labels above lock the `KNOCKOUT_ROUND_HINTS` "
        "list. Placeholder-name patterns (when present) lock the slot-label "
        "index strategy.",
        "2. **A.2 (PEN extraction)** — the sample PEN fixture shows whether "
        "`score.penalty.{home,away}` and `teams.{home,away}.winner` are "
        "populated as expected. If `winner` is missing, A.2 must derive it "
        "from pen sub-scores; if pen sub-scores are missing for some PEN "
        "matches, A.2 must surface a WARN rather than fabricate.",
        "3. **A.3 (simulator)** — confirms FT/AET/PEN are the three canonical "
        "locked statuses (matching `LOCKED_STATUSES` already in fetch_results.py).",
        "",
    ]

    return "\n".join(lines)


def main() -> int:
    key = get_key()
    headers = {
        "x-apisports-key": key,
        "Accept": "application/json",
        "User-Agent": "wc26-probe/1.0",
    }
    payloads: list[dict[str, Any]] = []
    for target in TARGETS:
        try:
            payload = probe_one(target, headers=headers)
            payloads.append(payload)
        except Exception as e:
            # One tournament failing shouldn't sink the whole probe — we
            # still want partial signal from whatever succeeded.
            print(f"[probe] {target['label']} FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            payloads.append({"_probe_metadata": {"tournament": target["label"], "error": str(e)}, "fixtures": []})

    summary = summarize(payloads)
    SUMMARY_PATH.write_text(summary)
    print(f"[probe] wrote {SUMMARY_PATH.relative_to(ROOT)} ({len(summary)} bytes)")
    print("\n=== PROBE COMPLETE ===")
    for p in payloads:
        m = p.get("_probe_metadata", {})
        print(f"  {m.get('tournament', '?')}: {m.get('knockout_fixtures_kept', 0)} knockout fixtures captured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
