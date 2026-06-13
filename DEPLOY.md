# Deployment runbook — fifa-wc-26-prediction

Follow these steps **in order**. Each block tells you (a) what to do, (b) which command to run, and (c) what success looks like. Anything tagged **MANUAL** needs you to click in the GitHub or Vercel UI — the rest is shell.

---

## 0. Sanity check (run from the repo root)

```bash
.venv/bin/python -m pytest tests/ -q          # 215+ tests pass (Stream B v2 hardening)
.venv/bin/python scripts/09_validate.py       # 37 / 37
.venv/bin/python scripts/pre_flight.py        # 217 / 217 — READY TO DEPLOY
node --check dashboard/app.js                 # JS syntax OK
```

If any of those fail, **stop**. Don't ship.

### Known follow-ups (post-tournament, non-blocking)

- **GitHub Actions Node 20 → newer LTS bump.** Workflows use `actions/checkout@v4`,
  `actions/setup-python@v5`, `actions/upload-artifact@v4` — all on Node 20.
  Node 20 LTS ends ~April 2026; GH Actions issues warning-only deprecation
  notices on the runner before any hard cutoff. Verify the latest stable
  major (likely v5/v6 of each action) and bump on a branch first, watch one
  cron cycle succeed, then merge. Don't bump mid-tournament — pipeline
  stability >  warning suppression. WC26 ends 19 Jul 2026.

---

## 1. Commit + merge feature branch into main

Currently every fix from this session is uncommitted on `feature/matchday-intelligence`. Two pieces of housekeeping must land on **`main`** for production behaviour, because:

- **GitHub Actions only runs scheduled crons from the default branch (`main`).**
- **Vercel production builds `main`.**

```bash
# 1a. Stage + commit the work on the feature branch.
git status --short
git add -A
git commit -m "round-2 fixes: P0-A CDN safeguard, P0-B FIFA Apr-2026, P1 a–i, P2 polish"

# 1b. Merge into main, fast-forward if possible.
git checkout main
git pull --ff-only origin main
git merge --no-ff feature/matchday-intelligence \
  -m "merge feature/matchday-intelligence: pre-tournament work order"

# 1c. After merge, confirm critical bits actually landed on main.
git show main:.github/workflows/live-matchday.yml      | grep -E "cron:|FEATURE_REF"
git show main:.github/workflows/matchday-intel-slow.yml | grep -E "cron:|FEATURE_REF"
git show main:.github/workflows/daily-baseline.yml      | grep -E "cron:|VERCEL_TOKEN"
git show main:dashboard/index.html                      | grep "_vercel/insights"
git show main:dashboard/app.js                          | grep "typeof Chart"
```

You should see:
- both `*/10 * 10-30 6 *` and `*/10 * 1-20 7 *` window-crons on `live-matchday.yml`
- both `0 */3 10-30 6 *` and `0 */3 1-20 7 *` on `matchday-intel-slow.yml`
- **no `FEATURE_REF:` line** anywhere on main (the personal-branch checkout trick must be gone)
- `_vercel/insights` tag on `index.html` (and `methodology.html`, `appendix.html`)
- `typeof Chart === 'undefined'` guard in `renderAllCharts`

```bash
# 1d. Push.
git push origin main
git push origin feature/matchday-intelligence    # keeps the branch in sync
```

---

## 2. GitHub repo secrets + variables

Open `https://github.com/<you>/fifa-wc-26-prediction/settings/secrets/actions` and confirm these. Anything missing? Add it.

| Kind     | Name                       | Purpose                                                  |
|----------|----------------------------|----------------------------------------------------------|
| Secret   | `API_FOOTBALL_KEY`         | Live results + injuries + lineups + match stats fetchers |
| Secret   | `FOOTBALL_DATA_TOKEN`      | Fallback results provider (optional)                     |
| Secret   | `SPORTMONKS_TOKEN`         | Optional alt provider                                    |
| Secret   | `VERCEL_TOKEN`             | CI deploy to Vercel (used by both workflows)             |
| Variable | `VERCEL_ORG_ID`            | Vercel team / personal-org id                            |
| Variable | `VERCEL_PROJECT_ID`        | This project's id                                        |
| Variable | `FOOTBALL_PROVIDER`        | `api_football` (or `mock` to disable live)               |
| Variable | `API_FOOTBALL_LEAGUE_ID`   | `1`                                                      |
| Variable | `API_FOOTBALL_SEASON`      | `2026`                                                   |

Quick CLI version (replace `<…>`):

```bash
gh secret set API_FOOTBALL_KEY    --body '<paste>'
gh secret set VERCEL_TOKEN        --body '<paste>'
gh variable set VERCEL_ORG_ID     --body '<paste>'
gh variable set VERCEL_PROJECT_ID --body '<paste>'
gh variable set FOOTBALL_PROVIDER --body 'api_football'
gh variable set API_FOOTBALL_LEAGUE_ID --body '1'
gh variable set API_FOOTBALL_SEASON    --body '2026'
```

Get a fresh Vercel token at <https://vercel.com/account/settings/tokens> (scope: full account or just this project). Get the org + project ids by running `vercel project ls` (or by opening the project → Settings → General → "Project ID").

---

## 3. Vercel — production branch, Deployment Protection, Analytics  **(MANUAL)**

The current preview URL `wc26-matchday-intelligence.vercel.app` is an auth-gated preview, which is the actual reason "the link doesn't work for outsiders". Three things to fix:

1. **Production branch = `main`**
   - Vercel dashboard → your project → Settings → Git → "Production Branch" → set to `main`.
2. **Deployment Protection → off for production** (or "Standard Protection — Bypass for…" if you only want previews gated)
   - Vercel dashboard → your project → Settings → Deployment Protection → Production: **Disabled**. Previews can stay protected.
3. **Enable Web Analytics + Speed Insights**
   - Vercel dashboard → your project → Analytics → click **Enable** (Web Analytics) and **Enable** (Speed Insights). The script tags are already in all three HTML pages; the toggle is purely server-side.

After step 3 the dashboard's `/_vercel/insights/view` beacon will start firing on every page view. To verify: open the prod URL in DevTools → Network → filter `insights` → reload → expect a `POST /_vercel/insights/view` returning `200`.

---

## 4. First production deploy

You have two paths. Pick one.

### 4a — push triggers Vercel Git integration (recommended)

`git push origin main` (already done in step 1d) → Vercel auto-builds main → deploys to your production domain. Watch the build at <https://vercel.com/<you>/fifa-wc-26-prediction/deployments>.

### 4b — direct CLI deploy

```bash
vercel pull   --yes --environment=production
vercel deploy --prod --yes
```

Once deployed, Vercel will also tell you the production URL (e.g. `https://fifa-wc-26-prediction.vercel.app`). Open it on your phone for the smoke test.

---

## 5. Smoke tests on prod

```bash
# 5a. Status JSON loads without auth.
curl -s -o /dev/null -w "%{http_code}\n" https://<your-prod>/live_state.json   # expect 200

# 5b. Manual workflow dispatch — dry-run so it doesn't write.
gh workflow run live-matchday.yml -f dry_run=true
gh workflow run matchday-intel-slow.yml -f dry_run=true
gh run watch
```

Both runs should go green end-to-end:
- Date gate: "Manual dispatch — bypassing date gate."
- Fetchers run with API key present.
- Validator: 37 / 37.

Then on your phone open the prod URL. Confirm:
- Hero renders.
- Live status bar appears (mode: "Pre-tournament static" or "Live-adjusted").
- Mobile nav scrolls horizontally to reach Methodology / Appendix.
- Methodology + Appendix pages: prose stays inside the viewport, no horizontal scroll on body.
- Open the Compare team picker — keyboard should not zoom on iOS (P1-B verification).

---

## 6. Once kickoff lands (11 Jun)

The live workflow will start firing every 10 minutes inside the window (10–30 Jun, 1–20 Jul). Things to keep an eye on:

- **First FT result**: the hero should flip to "Live-adjusted" within ~70 s of fetch. Top mover appears. The Featured tab in Matches now shows the locked card (P1-C).
- **Knockouts on 28 Jun**: the Matches tab continues to show fixtures with placeholder team labels ("1A" / "3A/B/C/D/F") until the group stage resolves; each card carries a stage chip ("Round of 32 · Inglewood, CA") (P1-D).
- **Live now strip**: appears above the hero when a match is in progress (P1-G). Reads "Live now · Mexico 1–0 South Africa · 38'".
- **Charts refresh**: the title-probability chart now updates on every tick alongside the table (P1-H).
- **Polling**: client downloads `live_state.json` (~0.2 KB) every 60 s and only re-fetches `predictions_live.json` (~19 KB gzip) when `last_updated_utc` changes (P1-F).

---

## 7. If something goes wrong

| Symptom                                | Where to look                                                  | Fix                                                                                                                           |
|----------------------------------------|----------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------|
| Live tab gets stuck                    | Actions tab on GitHub — last live-matchday run                 | Check `data/live/circuit_breaker_state.json` cache for `consecutive_failures >= 3`; delete the cache entry to reset.          |
| "Could not load data" error            | DevTools Console                                                | The narrow `init().catch` should only fire if predictions.json itself failed — check Network for `predictions.json` 200.       |
| Chart panels blank                     | Network — `cdn.jsdelivr.net`                                    | Expected if CDN blocked; you'll see a "Charts unavailable" note instead. Everything else still renders.                       |
| `manual / mock` shown when in live mode | Workflow secrets                                                | `API_FOOTBALL_KEY` missing or wrong; set the secret and re-dispatch.                                                          |
| README contender table looks stale     | Last daily-baseline run                                         | `scripts/10_regen_readme.py` regenerates between markers; if it didn't, the workflow's commit step skipped (no diff).         |

---

## 8. Post-launch backlog (deferred, with rationale)

These are flagged but not blocking the launch:

- **Recursive H2H tiebreaker** — FIFA regs re-apply the cascade among the remaining tied subset. Implemented as single-pass today; rare to hit in practice.
- **CSP `'unsafe-inline'` removal** — both inline `<script>` blocks can move to sha256-hashed sources after launch to drop the directive.
- **Venue-keyed knockout matrices** — current matrices are venue-agnostic (symmetric per H4). Per-slot venue + altitude + heat awareness is a model-quality improvement worth ~0.5–1 pp on bracket probabilities; matters most for the Mexico City matches at altitude.
- **Repo-weight plan** — daily-baseline commits ~3 MB of binaries per day (joblibs + parquet). Over the 39-day window that's ~120 MB of new git history; acceptable for the tournament. Post-tournament, prune or move models to `actions/cache` / Git LFS.
- **In-tournament Elo update** — pre-tournament Elo holds static across the 25k sims; `live_team_state` provides a soft delta (capped ±30 group, ±45 grand-total) but full Elo recomputation per-match is a follow-up.
