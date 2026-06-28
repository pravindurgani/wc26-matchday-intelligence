# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.0.1] — 2026-06-28 — "Knockout heartbeat"

Knockout-window operability patch. Resolves a false-PAUSED interaction
between the `live_state` deploy-churn guard and the Apps Script engine's
`STALE_MINUTES = 25` threshold; supersedes the "live_state churn skip"
behaviour listed under 3.0.0 (history preserved above).

### Changed
- `scripts/live/run_live_update.py:write_live_state` now bumps
  `last_updated_utc` on every successful tick. The previous 30-min
  preserve window (`HEARTBEAT_MAX_AGE_SECS`) overlapped the spreadsheet's
  25-min stale threshold and caused the engine to flip to
  "PAUSED · stale feed" between genuinely-quiet ticks. The small extra
  Vercel deploy cost is worth a true heartbeat.
- `dashboard/app.js` staleness comment updated to match (the 30/90-min
  client-side thresholds are unchanged).

### Added
- `*/30 * * * *` schedule trigger on `live-matchday.yml` as a backup
  safety net for the knockout window. Cloudflare Worker remains the
  primary scheduler (every 10 min via `workflow_dispatch`); the schedule
  only fills in if the Worker dies.
- Engine `v2.3.15` (wc26-engine-gs): `applyTailTrapGuardsToGroups` is
  now wired into `installEngine` so tail-trap guards apply on install
  rather than only on the first refresh.

## [3.0.0] — 2026-06-25 — "World Cup ready"

Tournament-ready release: full simulation pipeline, live matchday intelligence
layer, betting engine, and a 14-round pressure-test pass against R32.

### Added
- **Live matchday intelligence layer** (Stream B, 2026-06-09 → 2026-06-13):
  scaffold + audit log (B.1), weather via Open-Meteo (B.2), injuries via
  API-Football (B.3), lineup-change heuristic (B.4), post-match stats proxy
  (B.5), CWC2025 weather calibration (B.6), dashboard section (B.7), 3h slow
  cadence workflow (B.8), `pre_flight` Phase 12 integration (B.9).
- **Live results pipeline** (Stream A): knockout-aware fixture map for
  M73-M104 (A.1), PEN sub-score and AET/PEN winner extraction (A.2), simulator
  locks completed knockouts (A.3), workflow rebuilds fixture map when the
  knockout draw resolves (A.5), FT/AET/PEN rendering on the dashboard (A.6).
- **Live providers**: API-Football and football-data.org adapters with
  fixture-id maps and provider-switch auto-rebuild.
- **Betting engine** (`wc26-engine-gs/`, Apps Script v2.3.13) — gitignored.
- **Ops infrastructure**: launchd autopilot, Cloudflare Worker dispatcher,
  Vercel self-deploy, mobile-responsive dashboard, live polling.
- **CI safety**: scope-tagged calibration, deterministic `PYTHONHASHSEED`,
  simulator stability harness, sentinel propagation, whitelist refresh.

### Changed
- Hardening rounds R10-R14 (2026-06-17 → 2026-06-24): closed all HIGH/MED
  audit findings; fixed daily-baseline `max_g` drift, DOM leak, `og:image`
  absolute URL, live_state churn skip, dual-scheduler removal, 24h CF window.
- Matchday-intel hardening (2026-06-17): launchd path fix (H1), crash
  freshness (H2), retries (H3), rate-limit guard (H4).
- Robustness pass (2026-06-13): tz-aware weather, Dixon-Coles τ guard,
  matchday-intel hash fix, Patch Q CI gate.
- Weather horizon check uses UTC date to silence M73 `http_error` pill.

### Fixed
- CI: `[skip ci]` removed (was blocking Vercel redeploy of live JSON).
- CI: YAML repair after multi-line `python -c` broke workflow parsing.
- CI: `contents:write` permission for provider-switch fixture-map rebuilds.
- Vercel headers reordered so live JSON `max-age=0` wins over generic `.json`.

### Known limitations
True per-shot xG, per-player injury importance, refereeing patterns, and
news/social signals are out of scope. See `README.md` for the full list.

## [2.0.0] — 2026-06-10 — "Launch-eve hardening"

Launch-ready baseline immediately preceding the live matchday intelligence
layer. Four rounds of external audit closed before tournament start.

### Added
- Launch-eve workflow re-arm + Cloudflare Worker dispatcher.
- launchd autopilot for the preview link (`Choice 3`).
- Round-2 / round-4 launch audit fixes: P0/P1/P2 closures, DEPLOY runbook,
  All-104 label, appendix table-wrap, perf + security + a11y kickoff bundle.
- Live-mode foundation: API-Football fetcher, football-data.org adapter,
  fixture-id maps, live polling, mobile responsive layout.

## [1.0.0] — 2026-06-09 — "Launch-ready v3"

Initial public-ready release of the WC26 simulator.

### Added
- End-to-end pipeline: data ingest, XGBoost Poisson goal model, Monte Carlo
  bracket simulation, calibration, walk-forward backtest, 27-scenario
  sensitivity audit, ablation, travel-impact diff, pre-launch validator.
- Dixon-Coles + Negative Binomial scoreline model, Annex C bracket, FIFA 2026
  tiebreaker cascade, injury and travel-fatigue layers.
- Dashboard (`dashboard/`) with light/dark theme and p05/p95 ranges over 5
  seeds.

[3.0.1]: https://github.com/pravindurgani/wc26-matchday-intelligence/releases/tag/v3.0.1
[3.0.0]: https://github.com/pravindurgani/wc26-matchday-intelligence/releases/tag/v3.0.0
[2.0.0]: https://github.com/pravindurgani/wc26-matchday-intelligence/releases/tag/v2.0.0
[1.0.0]: https://github.com/pravindurgani/wc26-matchday-intelligence/releases/tag/v1.0.0
