# Personal-use live link — operator playbook

Your separate preview at **https://wc26-matchday-intelligence.vercel.app** is fed
by manual deploys from the `feature/matchday-intelligence` branch. Production
(`fifa-wc-26-prediction.vercel.app`) is on `main` and is **never touched** by
anything in this file.

## TL;DR — after every match you care about

```bash
cd ~/Desktop/personal-projects/fifa-wc-26-prediction

# 1. Edit data/live/results_2026.json — add the FT result. Example:
#    "completed_matches": [
#      {"m": 1, "date": "2026-06-11", "home_score": 2, "away_score": 0, "status": "FT"}
#    ]
# (Or skip this step if you have API_FOOTBALL_KEY set — the orchestrator will fetch.)

# 2. One command — runs the orchestrator, copies fresh JSON, validates, deploys, re-aliases:
./scripts/deploy_preview.sh
```

Open https://wc26-matchday-intelligence.vercel.app — fresh numbers + the live mode
flip + locked score on the M1 card within ~60 seconds.

---

## What's already auto-updating (no intervention needed)

The `matchday-intel-slow.yml` workflow on `main` (already set up in round 1)
runs every 3 hours, checks out the **feature branch**, runs the four
matchday-intel fetchers (injuries / weather / lineups / stats), and pushes
the resulting JSON back to the feature branch. Vercel's Git integration sees
the feature-branch push and auto-redeploys to your branch-preview URL.

So **weather, injuries, lineups, and stats** update on their own every 3 hours
during the tournament window. **Match results and live re-simulations do NOT**
— that's what the manual script above handles.

---

## What the script does (5 steps, all reversible)

1. **Live orchestrator** — `scripts/live/run_live_update.py`
   - Reads `data/live/results_2026.json` (or hits API-Football if key is set)
   - Hash-checks whether anything actually changed; skips re-sim if not
   - Re-runs `03_simulate.py --live` if results, intel, or team-state moved
   - Writes `predictions_live.json`, `live_state.json`, `live_delta.json`
2. **Sync JSON to dashboard/** — flat copy of the seven JSON files Vercel serves
3. **Validate** — `09_validate.py` (38/38 expected; the script halts on a fail)
4. **Vercel deploy** — `vercel deploy` (NO `--prod`)
5. **Re-alias** — points `wc26-matchday-intelligence.vercel.app` at the new preview

---

## Common situations

### Match just finished, you have the score
```bash
# Open data/live/results_2026.json in your editor.
# Append a record to "completed_matches" with status: "FT".
./scripts/deploy_preview.sh
```

### You want to push intel updates without waiting 3 hours
```bash
# Manually trigger the 3h workflow on main (it'll commit to feature branch):
gh workflow run matchday-intel-slow.yml
# Wait for it to finish, then:
git pull origin feature/matchday-intelligence
./scripts/deploy_preview.sh
```

### You only want to push a cosmetic dashboard tweak
```bash
# Edit dashboard/*.css or dashboard/*.html as needed
./scripts/deploy_preview.sh --skip-sim
```

### Smoke test before deploying
```bash
./scripts/deploy_preview.sh --dry-run
```

### Something went sideways and you want to roll back
```bash
# List recent deploys, pick a known-good one (by URL or timestamp):
vercel ls fifa-wc-26-prediction
# Re-alias the personal-link to that older deploy:
vercel alias set <old-preview-url> wc26-matchday-intelligence.vercel.app
```

### Choice 3 — full autopilot (NEVER touches main)
A macOS launchd agent on **your Mac** runs `deploy_preview.sh` every 15 min
during tournament + match windows. Zero changes to main, zero changes to
GitHub Actions, zero remote infra — just one plist in
`~/Library/LaunchAgents/`.

```bash
# Install (loads immediately, fires automatically every 15 min):
./scripts/launchd/install.sh

# Check status:
./scripts/launchd/install.sh status

# Watch ticks live:
tail -f logs/launchd-tick.log

# Disable:
./scripts/launchd/uninstall.sh
```

**Windowing** (in `scripts/launchd/run_if_tournament.sh`):
- Tournament: 11 Jun – 19 Jul 2026 only — outside this range every tick is a <1s no-op
- Match hours: 11:00–23:00 UTC — outside this range the tick skips
- Daily baseline: 05:00 UTC fires regardless of match-hour gate

**Why this beats "patch main workflows":** GitHub Actions `schedule:` only fires
from the default branch, so any cron approach requires patching main, which
you've explicitly ruled out. Local launchd sidesteps that limitation entirely.
The trade-off: it only fires when your Mac is awake. If you sleep through a
match, the deploy waits until the next wake-up — usually fine since the
state-first dashboard keeps polling and picks up changes the moment your Mac
catches up.

**Safety:** the agent calls `./scripts/deploy_preview.sh`, which deploys
**preview only** (no `--prod` flag) and re-aliases to
`wc26-matchday-intelligence.vercel.app`. Production at
`fifa-wc-26-prediction.vercel.app` is on `main` and is untouched by this
file or any agent it spawns.

---

## What will NOT work without the full-auto setup

- **Auto-recompute on FT** — every match end won't auto-refresh the dashboard
- **Daily-baseline retrain** — the overnight retrain only fires from main
- **Score-correction handling** — if a provider corrects a score, the personal
  link won't catch it until you re-run the script

If those matter to you during the first few days, fall back to running the
script after each match window closes (~once every 3–4 matches in group stage
is a reasonable cadence).

---

## Sanity checks before each major group of matches

```bash
.venv/bin/python -m pytest tests/ -q          # expect: 134 passed
.venv/bin/python scripts/09_validate.py       # expect: 38/38
.venv/bin/python scripts/pre_flight.py        # expect: 179/179 (READY)
node --check dashboard/app.js                 # expect: silent (OK)
```

If any of these fail, **don't deploy** — fix first. The validator is your
last line of defence against a broken JSON shipping to the personal link.

---

## How to read the preview URL state

The dashboard already shows you everything you need on-page:

- **"Updated …" top-right**: this is `live_state.last_updated_utc` from the
  most recent orchestrator run. If it's stale (yesterday's date), the script
  hasn't been re-run.
- **"PRE-TOURNAMENT STATIC" / "LIVE-ADJUSTED" mode pill**: live mode kicks
  in the moment `completed_matches_count > 0`. If you've added an FT result
  and the pill still says PRE-TOURNAMENT after deploy, the orchestrator
  didn't pick up the file — check `data/live/results_2026.json` syntax.
- **"LIVE now" red strip above hero** (P1-G): appears only when the
  orchestrator wrote any `in_play` matches. Disabled in mock mode unless
  you manually inject an `in_play` entry into `results_2026.json`.
- **Biggest movers section**: starts populating after the first FT result
  + re-sim. If empty after deploy, the re-sim didn't move anything ≥ 0.3pp
  (the configured threshold — see `build_live_delta()` docstring for the
  paired-SE math that picks 0.3pp).
