// wc26-dispatcher — Cloudflare Worker that pokes GitHub Actions on a
// schedule so the matchday preview keeps refreshing during the tournament
// without depending on the user's laptop being awake or GitHub's
// load-shed scheduler firing on time.
//
// Schedule defined in wrangler.toml: */10 * * * * (every 10 minutes, UTC).
// CF cron triggers are delivered reliably at the configured cadence and
// are independent of any laptop/server. Free tier covers this trivially.
//
// Secrets required (set via `wrangler secret put`):
//   GH_TOKEN    fine-grained PAT with Actions: Read and Write on the
//               wc26-matchday-intelligence repo. Expires 25 Jul 2026.
//   GH_OWNER    e.g. "pravindurgani"
//   GH_REPO     e.g. "wc26-matchday-intelligence"
//
// Optional env vars (set in wrangler.toml [vars]):
//   WORKFLOW    file name of the workflow to dispatch (default:
//               "live-matchday.yml")
//   REF         branch to dispatch on (default: "main")
//   WINDOW_START_UTC_DATE  earliest date to fire (default: "2026-06-11")
//   WINDOW_END_UTC_DATE    latest date to fire   (default: "2026-07-19")
//   WINDOW_HOUR_FROM       first UTC hour to fire (default: 4)
//   WINDOW_HOUR_TO         last UTC hour to fire  (default: 23)
//
// Why a window? Match action runs 11–23 UTC; daily-baseline at 05 UTC.
// Outside that range the dispatch is a no-op anyway (orchestrator
// early-exits on identical input hash), but skipping saves Actions
// minutes + Vercel deploys. Easy to widen by editing vars.

export default {
  async scheduled(event, env, ctx) {
    const now = new Date(event.scheduledTime);
    const ymd = now.toISOString().slice(0, 10);          // "2026-06-11"
    const hour = now.getUTCHours();                       // 0..23

    const startDate = env.WINDOW_START_UTC_DATE || "2026-06-11";
    const endDate   = env.WINDOW_END_UTC_DATE   || "2026-07-19";
    const hourFrom  = parseInt(env.WINDOW_HOUR_FROM ?? "4", 10);
    const hourTo    = parseInt(env.WINDOW_HOUR_TO   ?? "23", 10);

    if (ymd < startDate || ymd > endDate) {
      console.log(`skip: outside tournament window (${ymd})`);
      return;
    }
    if (hour < hourFrom || hour > hourTo) {
      console.log(`skip: outside match-hour window (${hour} UTC)`);
      return;
    }

    const workflow = env.WORKFLOW || "live-matchday.yml";
    const ref      = env.REF      || "main";
    const owner    = env.GH_OWNER;
    const repo     = env.GH_REPO;
    if (!owner || !repo || !env.GH_TOKEN) {
      console.error("missing GH_OWNER, GH_REPO, or GH_TOKEN secret/var");
      return;
    }

    const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.GH_TOKEN}`,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "wc26-dispatcher",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref }),
    });
    const status = resp.status;
    if (status === 204) {
      console.log(`dispatch ok: ${owner}/${repo} ${workflow} @ ${ref}`);
    } else {
      const body = await resp.text();
      console.error(`dispatch FAIL ${status}: ${body.slice(0, 500)}`);
    }
  },

  // Optional: hitting the worker URL directly returns a heartbeat so you can
  // spot-check that secrets resolve without waiting for the next cron tick.
  async fetch(request, env) {
    const ok = !!(env.GH_TOKEN && env.GH_OWNER && env.GH_REPO);
    return new Response(
      JSON.stringify({
        worker: "wc26-dispatcher",
        secrets_present: ok,
        workflow: env.WORKFLOW || "live-matchday.yml",
        ref: env.REF || "main",
        window: {
          start: env.WINDOW_START_UTC_DATE || "2026-06-11",
          end: env.WINDOW_END_UTC_DATE || "2026-07-19",
          hours: `${env.WINDOW_HOUR_FROM ?? 4}-${env.WINDOW_HOUR_TO ?? 23} UTC`,
        },
        now_utc: new Date().toISOString(),
      }, null, 2),
      { headers: { "Content-Type": "application/json" } }
    );
  },
};
