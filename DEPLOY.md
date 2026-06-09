# Deploy Guide — World Cup 2026 Simulator

This is the verified, step-by-step launch sequence. Run the pre-flight first.
If every phase is green, the deploy is safe.

```bash
python3 scripts/pre_flight.py     # must end in: ✓ READY TO DEPLOY
```

---

## 0 · One-time prerequisites

- **GitHub account** (you already have one).
- **Git** installed and authenticated: `git --version`.
- **Vercel CLI** (latest):

  ```bash
  npm i -g vercel@latest        # or: pnpm add -g vercel@latest
  vercel --version              # should be 54.10+ (you currently have 54.9.1)
  ```

- **Vercel account** — sign in with GitHub at <https://vercel.com/login>.

You do **not** need a domain to deploy. Vercel gives you
`world-cup-2026-simulator.vercel.app` (or similar) for free. A custom domain is
optional and covered in step 4.

---

## 1 · Initialise the local git repo

You're not on a repo yet. Run, from the project root:

```bash
cd /Users/prav/Desktop/personal-projects/fifa-wc-26-prediction

git init
git branch -M main

# Sanity: confirm .gitignore is the strict one
head -40 .gitignore
```

The `.gitignore` should already exclude `.venv/`, `.claude/`, `.gstack/`,
`__pycache__/`, `.DS_Store`, `__MACOSX/`, and the runtime
`circuit-breaker-state.json` / `circuit_breaker_state.json`. Pre-flight Phase 1
verifies this.

Stage everything ignoring junk:

```bash
git add .
git status                         # eyeball — must NOT show .venv, .gstack, .claude
git commit -m "feat: launch-ready World Cup 2026 simulator (v3)"
```

If you see any local artefacts staged, **stop**: re-check `.gitignore` and run
`git restore --staged <path>` on the offender.

---

## 2 · Push to GitHub (public repo)

### 2a · Create the remote with the GitHub CLI (fastest)

```bash
# Requires `gh` — install with `brew install gh` if missing, then `gh auth login`
gh repo create fifa-wc-26-prediction \
  --public \
  --source=. \
  --remote=origin \
  --description "Probabilistic FIFA World Cup 2026 simulator — 25,000 Monte Carlo runs, NB goal model + Dixon-Coles, live update orchestrator." \
  --push
```

That single command creates the repo on github.com, sets `origin`, and pushes
`main`. Skip to step 3.

### 2b · Or create the repo manually on github.com

1. Go to <https://github.com/new>.
2. Owner = your username, name = `fifa-wc-26-prediction`, visibility = **Public**.
3. **Do not** initialise with README / .gitignore / license (we already have them).
4. Click *Create repository*.
5. Back in your terminal:

```bash
git remote add origin git@github.com:<your-username>/fifa-wc-26-prediction.git
git push -u origin main
```

Verify on github.com that `.venv/`, `.gstack/`, and `.claude/` are **not** there.

---

## 3 · Deploy to Vercel

### 3a · Recommended: link via Vercel CLI

From the repo root:

```bash
vercel login                       # one-time
vercel link                        # creates .vercel/project.json
vercel --prod                      # deploys to production
```

When `vercel link` asks:

- *Set up and link* → **Y**
- *Which scope?* → your personal account
- *Link to existing project?* → **N**
- *Project name?* → `fifa-wc-26-prediction` (or any slug you like)
- *In which directory is your code located?* → `./` (press enter)
- *Want to override settings?* → **N** (we have `vercel.json`)

`vercel.json` already pins:

- `outputDirectory: dashboard`
- `cleanUrls: true`
- Aggressive `Cache-Control: max-age=0, must-revalidate` on
  `predictions.json` / `predictions_live.json` / `live_state.json` /
  `live_delta.json` so the live feed never goes stale.
- Standard security headers.

When `vercel --prod` finishes you get a URL like
`https://fifa-wc-26-prediction.vercel.app`.

### 3b · Or deploy directly from GitHub (no CLI)

1. <https://vercel.com/new>.
2. *Import Git Repository* → pick `fifa-wc-26-prediction`.
3. *Framework Preset* → **Other**.
4. *Root Directory* → `./` (leave default).
5. *Build & Output Settings* → leave on Vercel defaults; `vercel.json` is read.
6. Click **Deploy**.

Auto-redeploy on push is enabled by default — every `git push origin main`
ships a new production build.

---

## 4 · (Optional) Add a custom domain

If you want `world-cup-2026.com` or `worldcup26.yourdomain.com` instead of
the Vercel subdomain.

### Where to buy

| Registrar           | Pricing (.com)   | Pros                                       | Notes |
|---------------------|------------------|--------------------------------------------|-------|
| **Cloudflare**      | ~$10/yr at cost  | At-cost, no markup, instant DNS            | Best value if you want pure ownership |
| **Vercel Domains**  | ~$15/yr          | One-click attach, no DNS to configure      | Best UX, slight premium |
| **Namecheap**       | ~$10–15/yr       | Familiar, free WHOIS privacy               | Solid default |
| **Porkbun**         | ~$9/yr           | Cheap, decent UX                           | Less mainstream |

**Recommendation:** buy at **Vercel Domains** if you want zero DNS work, or
**Cloudflare Registrar** if you're optimising for price + want CF's DNS.

### Wire the domain

Once purchased (assume `yourdomain.com`):

1. In the Vercel dashboard → your project → **Settings → Domains** → *Add*.
2. Enter `yourdomain.com` and `www.yourdomain.com`.
3. Vercel shows you the DNS records you need (one of):
   - `A` record on `@` → `76.76.21.21`
   - `CNAME` record on `www` → `cname.vercel-dns.com`
4. Add those records at your registrar's DNS panel (or, if you bought through
   Vercel Domains, this is automatic).
5. Wait for the SSL provision indicator to turn green — usually < 5 minutes.
6. Test: open `https://yourdomain.com`.

For a tournament-only standalone URL, something like `wc26.<yourdomain>`,
`fifa26sim.com`, or `montecarlowc.com` reads well.

---

## 5 · Verify the live deployment

Once Vercel reports "Ready", run a manual smoke test against the deployed URL.
Replace `<URL>` with what Vercel returned.

```bash
URL=https://fifa-wc-26-prediction.vercel.app

# Pages
for path in "/" "/methodology" "/appendix"; do
  code=$(curl -sw "%{http_code}\n" -o /dev/null "$URL$path")
  echo "$code $path"
done

# JSON
for f in predictions.json predictions_live.json live_state.json live_delta.json \
         calibration.json walk_forward.json ablation.json sensitivity.json \
         travel_impact.json; do
  code=$(curl -sw "%{http_code}\n" -o /dev/null "$URL/$f")
  echo "$code /$f"
done

# Cache headers on the live file
curl -sI "$URL/predictions.json" | grep -i cache-control
# expect: cache-control: public, max-age=0, must-revalidate, s-maxage=60, stale-while-revalidate=120
```

In a browser, also test:

- `<URL>/#team=Spain` → contender drawer auto-opens.
- `<URL>/#group=D` → group filter applies.
- `<URL>/#compare=Brazil,France` → compare panel populates.
- `<URL>/#team=Atlantis` → silently ignored, no console error.
- Mobile width ~390px → no horizontal scroll, tables still readable.
- DevTools → Console → no errors on load.

---

## 6 · Turn on live updates during the tournament

The live pipeline has two modes. **Manual / mock** is the default and always
works without any external service. **API-Football** is the real-provider mode
— the adapter is fully wired; you just need a key + one CI secret.

### Option A — manual / mock mode (default, zero cost)

Edit `data/live/results_2026.json` after each match and push:

```bash
# After Mexico vs South Africa finishes 2-0:
edit data/live/results_2026.json
git commit -am "live: lock M1 (Mexico 2-0 South Africa)"
git push
```

The `live-matchday` workflow runs every 10 minutes (11 Jun → 19 Jul 2026),
detects the new locked match, re-simulates, and commits updated JSON. The
dashboard label reads `provider: manual / mock`.

### Option B — activate API-Football (recommended for the tournament)

**Total time:** ~10 minutes once you have the key.

#### Step 1 — Get the key

1. Sign up at <https://www.api-football.com/> (free tier = 100 requests/day,
   plenty for 10-minute polling during a 38-day tournament).
2. From your API-Football dashboard, copy the API key.
3. Find the FIFA World Cup 2026 league id — it appears in the dashboard's
   "Coverage / Leagues" section. The default is `1`; if API-Football uses a
   different id for WC26 specifically, copy that one.

#### Step 2 — Configure the repo

In the GitHub repo: **Settings → Secrets and variables → Actions**

Add **Secret** (encrypted, never exposed in logs):
- `API_FOOTBALL_KEY` = `<paste your key>`

Add **Variable** (plain, visible in workflow logs):
- `FOOTBALL_PROVIDER` = `api_football`
- *(Optional)* `API_FOOTBALL_LEAGUE_ID` = the WC2026 league id (default `1`)
- *(Optional)* `API_FOOTBALL_SEASON` = `2026` (default already correct)

#### Step 3 — Build the fixture map (one-time, local)

The fixture map gives `fetch_results.py` an O(1) `provider_fixture_id → match_id`
lookup, so we don't fuzzy-match teams every tick.

```bash
export FOOTBALL_PROVIDER=api_football
export API_FOOTBALL_KEY="<your key>"

# Dry-run first — prints what would map without writing anything:
python3 scripts/live/build_provider_fixture_map.py --provider api_football

# Inspect the output. It must say "✓ all 72 group fixtures mapped".
# If any are unmapped, add the missing team alias to TEAM_ALIAS
# (scripts/live/fetch_results.py) and re-run.

# Once 72/72 maps, write the file:
python3 scripts/live/build_provider_fixture_map.py --provider api_football --write
```

This writes `data/live/provider_fixture_map.json` with all 72 group fixtures
mapped to provider IDs. **Commit it to the repo** — it's deterministic input
data, not runtime state.

```bash
git add data/live/provider_fixture_map.json
git commit -m "feat: API-Football fixture-id map for WC2026"
git push
```

#### Step 4 — Dry-run the orchestrator (no commits, no sim)

```bash
python3 scripts/live/run_live_update.py --provider api_football --dry-run
```

You should see:

```
[fetch_results] provider=api_football (dry-run)
[fetch_results] GET https://v3.football.api-sports.io/fixtures?league=1&season=2026
[fetch_results] API-Football returned 72 fixtures
[dry-run] status distribution: {'NS': 72}     # all not-started, pre-kickoff
[fetch_results] valid=0 rejected=0 warnings=0
[fetch_results] dry-run — no file written
```

If you see HTTP errors (401, 403, 429), the key is wrong or rate-limited —
fix at API-Football's dashboard.

#### Step 5 — Trigger the workflow manually

```bash
gh workflow run live-matchday.yml -f provider=api_football -f dry_run=true
gh run watch
```

If the dry-run succeeds, trigger a real run:

```bash
gh workflow run live-matchday.yml
gh run list --workflow live-matchday.yml --limit 3
```

#### Step 6 — Verify

After the run completes (~1 min), check:

```bash
curl -s https://fifa-wc-26-prediction.vercel.app/live_state.json | python3 -m json.tool
```

You should see:

```json
{
  "mode": "pre_tournament",
  "completed_matches_count": 0,
  "source": "api_football",
  "provider_mode": "active",
  "warnings": [],
  ...
}
```

The dashboard's status badge now reads
**`provider: API-Football (live)`** instead of `manual / mock`.

#### What's hardened (so you don't get woken up at 3 AM)

- **Atomic writes** — every JSON file goes via `.tmp + rename`. Browsers never
  see half-written data.
- **Refuse-to-shrink** — if the provider returns 5 locked matches but
  `results_2026.json` already has 12, the fetcher refuses (assumes partial
  fetch / auth issue) and preserves the existing file.
- **Circuit breaker** — 3 consecutive sim failures and the orchestrator stops
  trying. Delete `data/live/circuit_breaker_state.json` to reset.
- **Status filter** — only `FT`, `AET`, `PEN` lock. `HT` / `2H` / `LIVE` /
  in-progress states are skipped silently. `POSTPONED` / `ABANDONED` /
  `SUSPENDED` / `WALKOVER` go to `live_state.warnings` and never lock.
- **Date tolerance ±1 day** — handles UTC↔local boundary for NA evening
  kickoffs.
- **Graceful fallback** — if `API_FOOTBALL_KEY` is missing or the API errors
  out, the orchestrator falls back to mock mode without crashing CI.

#### Falling back

If API-Football has an outage or you want to pause auto-updates, just unset
the variable:

```
GitHub → Settings → Variables → FOOTBALL_PROVIDER = mock
```

The next tick falls back to manual mode and the dashboard label updates
within 10 minutes.

---

## 7 · Vercel preview deployments per branch (recommended)

Every push to a non-`main` branch becomes a free preview URL:

```bash
git checkout -b feature/xyz
# edit edit edit
git push -u origin feature/xyz
# Vercel posts a preview URL on the PR
```

Use this for any change after launch — never edit `main` directly during a
live tournament.

---

## 8 · Pre-launch final checklist (run this RIGHT BEFORE you tweet the URL)

```bash
# 1. Pre-flight
python3 scripts/pre_flight.py
# must end in: ✓ READY TO DEPLOY

# 2. Local serve & manual eyeball at 390 / 768 / 1440px
cd dashboard && python3 -m http.server 8765 &
open "http://localhost:8765/"

# 3. Deployed-URL smoke test (see step 5 above)

# 4. Console clean on the production URL (DevTools → Console)

# 5. Share-link sanity:
#    - <URL>/#team=Spain
#    - <URL>/#group=D
#    - <URL>/#compare=Spain,Argentina
#    - <URL>/methodology
#    - <URL>/appendix
```

Soft launch:

1. Share with 3–5 friends/colleagues for 24h.
2. Watch their console / phone results.
3. Then announce publicly.

---

## Domain-name recommendations

If you want a tournament-specific URL:

- `wc26-sim.com`
- `worldcup26.app`
- `fifa26forecast.com` *(probably trademark-flagged — avoid)*
- `monte-carlo-wc.com`
- `worldcupodds26.com`

If you want it under your portfolio:

- `wc26.pravindurgani.com` *(subdomain — free at Vercel/Cloudflare)*
- `simulator.pravindurgani.com`

Subdomains under a domain you already own cost nothing extra. Recommended
default: `wc26.pravindurgani.com` until you decide whether a standalone domain
is worth the spend.

---

## What to do if pre-flight fails

`pre_flight.py` prints the exact list of failed checks. Common ones:

| Failure | Fix |
|---|---|
| `no .DS_Store inside dashboard/` | `find dashboard -name ".DS_Store" -delete` |
| `node --check app.js` | open `dashboard/app.js`, paste any error line/col |
| `Σ p_champion ≈ 1.0` | rerun `scripts/03_simulate.py --seeds 5 --sims 5000` |
| `live_state.mode is valid` | check `dashboard/live_state.json` was regenerated |
| `index.html no '<overclaim>'` | reword to probabilistic phrasing |

After every fix re-run `python3 scripts/pre_flight.py` until you see
`✓ READY TO DEPLOY`. Then proceed with step 2.

---

## TL;DR — the four-command launch

```bash
python3 scripts/pre_flight.py                 # 1. verify
gh repo create fifa-wc-26-prediction \
  --public --source=. --remote=origin --push  # 2. github
vercel link && vercel --prod                  # 3. vercel
curl -sI "$URL/predictions.json" \
  | grep cache-control                        # 4. confirm cache
```

That's it. Tweet the URL.
