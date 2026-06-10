# wc26-matchday-intelligence — runbook

This repo is the standalone home for **https://wc26-matchday-intelligence.vercel.app/**.
It is completely separate from `pravindurgani/fifa-wc-26-prediction`
(the original production simulator).

## Architecture

```
        Cloudflare Worker  (cron */10 * * * *)
                │
                │ workflow_dispatch (POST to GitHub API)
                ▼
   GitHub Actions: pravindurgani/wc26-matchday-intelligence
       • live-matchday.yml     — every 10 min (also has native schedule)
       • matchday-intel-slow   — every 3 h
       • daily-baseline        — 05:00 UTC daily
                │
                │ orchestrator → commit results to main
                ▼
        Vercel (Git integration on `wc26-matchday-intelligence`)
                │
                │ auto-deploy on every push to main
                ▼
   https://wc26-matchday-intelligence.vercel.app/   (PUBLIC, no SSO)
```

Two redundant schedulers:

1. **Cloudflare Worker** (`wc26-dispatcher.pdurgani6.workers.dev`):
   reliable cron, fires every 10 min, gates on tournament window
   2026-06-11 → 2026-07-19 and UTC hours 04–23.
2. **GitHub Actions native `schedule:`** in `live-matchday.yml`:
   `*/10 * 11-30 6 *` + `*/10 * 1-19 7 *` — backup scheduler in case
   the CF Worker is interrupted.

Both dispatch the same workflow. GitHub Actions' `concurrency: wc26-live`
group de-dupes any overlap.

## Failure / debug paths

| Symptom | Where to look | Fix |
|---|---|---|
| Preview URL serves old data | `gh run list -R pravindurgani/wc26-matchday-intelligence -L 5` | Re-dispatch: `gh workflow run "Live matchday refresh" -R pravindurgani/wc26-matchday-intelligence` |
| `provider_mode: manual` on `live_state.json` | Repo secret `API_FOOTBALL_KEY` unset or expired | Reset: `gh secret set API_FOOTBALL_KEY -R pravindurgani/wc26-matchday-intelligence` |
| Deploy step skipped in workflow runs | Repo secret `VERCEL_TOKEN` unset | Reset: `gh secret set VERCEL_TOKEN -R pravindurgani/wc26-matchday-intelligence` |
| CF Worker not firing | `wrangler tail` from `cf-worker/` shows logs | Re-deploy: `cd cf-worker && wrangler deploy` |
| `dispatch FAIL 401` in CF Worker logs | `GH_TOKEN` PAT expired or revoked | Regenerate PAT (Actions: R/W), `wrangler secret put GH_TOKEN` |
| `dispatch FAIL 404` | `GH_OWNER`/`GH_REPO`/workflow file name mismatch | `wrangler secret put GH_OWNER` / `GH_REPO` |
| Vercel not auto-deploying on push | Git integration broke | Reconnect: `vercel git connect https://github.com/pravindurgani/wc26-matchday-intelligence.git --yes` |

## Smoke-test commands

```bash
# 1. Preview URL responds publicly (no SSO):
curl -sI https://wc26-matchday-intelligence.vercel.app/ | head -2

# 2. Predictions valid:
curl -s https://wc26-matchday-intelligence.vercel.app/predictions.json \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(len(d['match_predictions']),'matches')"

# 3. Live state freshness:
curl -s https://wc26-matchday-intelligence.vercel.app/live_state.json \
  | python3 -m json.tool

# 4. CF Worker heartbeat (secrets present + window config):
curl -s https://wc26-dispatcher.pdurgani6.workers.dev | python3 -m json.tool

# 5. Manual workflow dispatch:
gh workflow run "Live matchday refresh" --ref main \
  -R pravindurgani/wc26-matchday-intelligence
```

## Manual operations

```bash
# Override results for a match (if provider missed it):
# Edit data/live/results_2026.json with the FT result, then:
gh workflow run "Live matchday refresh" --ref main \
  -R pravindurgani/wc26-matchday-intelligence

# Re-run the daily baseline retrain (full sim refresh):
gh workflow run "Daily baseline refresh" --ref main \
  -R pravindurgani/wc26-matchday-intelligence

# Force a matchday-intel refresh outside the 3h cadence:
gh workflow run "Matchday intelligence (slow cadence)" --ref main \
  -R pravindurgani/wc26-matchday-intelligence
```

## Shutdown after the tournament (post 2026-07-20)

```bash
# Disable the CF Worker:
cd cf-worker && wrangler delete

# (Optional) Delete the repo + Vercel project:
gh repo delete pravindurgani/wc26-matchday-intelligence
vercel project rm wc26-matchday-intelligence
```

The `wc26-dispatcher` PAT auto-expires 2026-07-25 — no manual revocation
needed. The fine-grained PAT only has Actions:R/W on this single repo, so
zero blast radius even if leaked.

## Isolation from `fifa-wc-26-prediction`

This repo is fully independent:

- Different GitHub repo (`pravindurgani/wc26-matchday-intelligence`)
- Different Vercel project (`prj_Fb8FwiirJJbSJgdzNzVdC4BEetN2`)
- Different domain (`wc26-matchday-intelligence.vercel.app`)
- Different cron schedulers (CF Worker, native GHA cron from this repo)
- No shared workflows, no shared secrets, no git remote overlap

Production at `fifa-wc-26-prediction.vercel.app` is untouched by every
code path in this repo. Verified by checking prod `live_state.json` ts
remained unchanged through the entire migration.
