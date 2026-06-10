# wc26-dispatcher — Cloudflare Worker

A 10-line worker that POSTs `workflow_dispatch` to the GitHub repo every
10 minutes so the matchday preview refreshes reliably during the
tournament, without depending on a laptop being awake or GitHub
Actions' load-shed scheduler firing on time.

## One-time setup (15 minutes)

### 1. Fine-grained GitHub PAT

GitHub → Settings → Developer settings → **Personal access tokens** →
**Fine-grained tokens** → Generate new token.

- **Name**: `wc26-dispatcher`
- **Expiration**: `2026-07-25` (one week after the final — auto-cleanup)
- **Resource owner**: `pravindurgani`
- **Repository access**: Only select repositories → `wc26-matchday-intelligence`
- **Permissions** → Repository permissions:
  - **Actions**: Read and write *(this alone is enough; nothing else needed)*

Click "Generate token" and copy the `github_pat_…` string. You'll paste
it into wrangler in step 4.

### 2. Cloudflare account + wrangler

If you don't have one: `https://dash.cloudflare.com/sign-up` (free).
Then locally:

```bash
npm install -g wrangler   # or: bun add -g wrangler
wrangler login            # opens browser to authorize
```

### 3. Deploy

From the repo root:

```bash
cd cf-worker
wrangler deploy
```

Wrangler reads `wrangler.toml`, uploads `worker.js`, and registers the
`*/10 * * * *` cron. Output includes the worker's public URL like
`https://wc26-dispatcher.<your-subdomain>.workers.dev`.

### 4. Set secrets

```bash
wrangler secret put GH_TOKEN
# Paste the github_pat_… from step 1.
wrangler secret put GH_OWNER
# Type: pravindurgani
wrangler secret put GH_REPO
# Type: wc26-matchday-intelligence
```

### 5. Smoke-test

```bash
curl https://wc26-dispatcher.<your-subdomain>.workers.dev
```

Should return JSON with `secrets_present: true`. If `false`, re-check
step 4.

### 6. Trigger a tick now (optional)

```bash
wrangler tail   # in one terminal — streams worker logs
# in another terminal:
curl -X POST https://api.cloudflare.com/client/v4/accounts/<acct-id>/workers/scripts/wc26-dispatcher/schedules \
     -H "Authorization: Bearer <cf-token>" \
     -d '{"cron":"*/10 * * * *"}'
```

Easier: just wait for the next 10-min boundary. The worker logs
`dispatch ok: …` on success, or `dispatch FAIL: …` if the PAT is wrong.

## Windowing

Defaults skip dispatches outside `2026-06-11`–`2026-07-19`, `04–23 UTC`.
The orchestrator early-exits on no-op anyway, but skipping at the worker
level saves GitHub Actions minutes + Vercel deploys.

Override via `wrangler.toml [vars]` and re-deploy:

```toml
[vars]
WINDOW_HOUR_FROM = "0"   # widen to 24h
WINDOW_HOUR_TO   = "23"
```

## After the tournament

```bash
wrangler delete         # remove the worker
```

PAT auto-expires 2026-07-25 (per step 1). Both gone, no cleanup debt.

## Tradeoffs vs alternatives

| Option | Pro | Con |
|---|---|---|
| **CF Worker cron (this)** | reliable delivery, free, laptop-independent, sub-minute deploys | requires CF account |
| GitHub Actions `schedule:` from main | zero infra | load-shed during peak — historically ~4 runs / 12h vs 72 expected |
| macOS launchd on laptop | zero accounts needed | Mac must be awake; TCC blocks on `~/Desktop` |
| External cron service (cron-job.org, EasyCron) | simpler than CF | rate-limited free tier, account churn |

CF Worker is the only option that satisfies all three: reliable +
free + laptop-independent.
