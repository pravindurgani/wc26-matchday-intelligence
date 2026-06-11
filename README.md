# FIFA World Cup 2026 — AI Prediction Dashboard (v3)

End-to-end probabilistic simulator for the 2026 World Cup. Survived three rounds
of independent expert review. Production-ready: light/dark theme, multi-seed CIs,
calibration audit, walk-forward backtests, sensitivity analysis, travel fatigue,
injury layer, live-mode foundation, pre-launch validation script.

**Live dashboard**: deploy with `vercel deploy` or push to GitHub Pages from `dashboard/`.

## What's in the box

| Layer | What it does |
|---|---|
| `scripts/01_prepare_data.py` | Ingest 49k international matches, normalize team names, compute Elo |
| `scripts/02_goal_model.py` | Train two XGBoost Poisson regressors (home/away goals) |
| `scripts/03_simulate.py` | Monte Carlo sim — Annex C bracket, NB+Dixon-Coles, travel, injuries, live mode |
| `scripts/04_evaluate.py` | Calibration + holdout backtest |
| `scripts/05_sensitivity.py` | 22-scenario sensitivity audit |
| `scripts/06_ablation.py` | Elo-only vs goal-model lift |
| `scripts/07_walk_forward.py` | Walk-forward backtest on WC 2010/14/18/22 |
| `scripts/08_travel_impact.py` | Diff travel-on vs travel-off, output travel_impact.json |
| `scripts/09_validate.py` | Pre-launch validator (versions, sims, JSON integrity, secrets) |
| `scripts/tiebreakers.py` | Full FIFA 2026 tiebreaker cascade |

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Full pipeline (~5 minutes total)
.venv/bin/python scripts/01_prepare_data.py
.venv/bin/python scripts/02_goal_model.py
.venv/bin/python scripts/04_evaluate.py
.venv/bin/python scripts/06_ablation.py
.venv/bin/python scripts/07_walk_forward.py
.venv/bin/python scripts/05_sensitivity.py
.venv/bin/python scripts/03_simulate.py --no-travel --out predictions_no_travel.json
.venv/bin/python scripts/03_simulate.py
.venv/bin/python scripts/08_travel_impact.py

# Copy artifacts to dashboard
for f in predictions calibration travel_impact; do cp data/processed/${f}.json dashboard/; done
for f in walk_forward ablation sensitivity; do cp models/${f}.json dashboard/; done

# Pre-launch validation — must pass before publishing
.venv/bin/python scripts/09_validate.py

# Serve locally
cd dashboard && python3 -m http.server 8765
# → http://localhost:8765
```

## CLI flags

```bash
python scripts/03_simulate.py --quick           # 3 seeds × 2k sims (smoke test, ~10s)
python scripts/03_simulate.py --seeds 5 --sims 5000   # production (~30s)
python scripts/03_simulate.py --no-travel       # disable travel fatigue (for impact diff)
python scripts/03_simulate.py --no-dispersion   # Poisson instead of Negative Binomial
python scripts/03_simulate.py --no-adjustments  # ignore injury layer
python scripts/03_simulate.py --live --out predictions_live.json   # live mode w/ locked completed matches
```

## Going live

**Option 1: Vercel (recommended)** — `vercel.json` provided.
```bash
npm i -g vercel
vercel deploy --prod
```

**Option 2: GitHub Pages** — push to a repo, enable Pages from `dashboard/`.

**Daily refresh**: `.github/workflows/daily-baseline.yml` runs the full pipeline once a
day at 05:00 UTC — validator gate, then commits the regenerated model + parquet +
dashboard JSON back to the repo. When `VERCEL_TOKEN` / `VERCEL_ORG_ID` /
`VERCEL_PROJECT_ID` are set, the workflow also runs `vercel deploy --prod` directly.
**Live matchday refresh** (`live-matchday.yml`) polls every 10 minutes during the
10 Jun – 20 Jul window, hash-gated to re-simulate only when results, matchday intel,
or team-state actually changed.

## Live mode (during the tournament)

Edit `data/live/results_2026.json` to add completed match scorelines:

```json
{
  "completed_matches": [
    {"m": 1, "date": "2026-06-11", "home_score": 2, "away_score": 0},
    {"m": 2, "date": "2026-06-11", "home_score": 1, "away_score": 1}
  ]
}
```

Then `python scripts/03_simulate.py --live --out predictions_live.json` — locked matches
are used verbatim, future matches simulated with updated state.

## Matchday intelligence (live)

A 3h GitHub Actions workflow (`matchday-intel-slow.yml`) fans out to four
fetchers and consolidates the results into a single per-team Elo adjustment
the simulator picks up via `apply_matchday_adjustments.get_team_elo_adjustment()`.

| Layer        | Source                                  | Cap (Elo)        | File                                  |
|--------------|-----------------------------------------|------------------|---------------------------------------|
| Injuries     | API-Football `/injuries`                | ±25 / extreme ±35 | `data/live/injuries_2026.json`        |
| Weather      | Open-Meteo (16-day forecast + climate fallback) | ±15      | `data/live/weather_2026.json`         |
| Lineups      | API-Football `/fixtures/lineups`        | ±20              | `data/live/lineups_2026.json`         |
| Stats proxy  | API-Football `/fixtures/statistics` (NOT xG) | ±8/match, ±20/group | `data/live/match_stats_2026.json` |
| Aggregate    | sum across layers                       | **±35 / team / match** | `dashboard/matchday_intelligence.json` |
| Grand total  | + mid-tournament live form delta        | **±45**          | (enforced in simulator) |

Every per-tick decision is appended to `data/live/matchday_intelligence_log.jsonl`
so any probability move can be traced back to the row that triggered it.

Manual operator overlay (`data/live/team_adjustments.json`) still stacks on
top of the API auto-feed — used for tier-1 player calls API-Football doesn't
disambiguate (Mbappé / Bellingham / Rodri-tier players warrant -30 rather than
the default -12 starter penalty). Schema:

```json
{
  "adjustments": [
    {"team": "France", "player": "Kylian Mbappé", "status": "out",
     "adjustment_elo": -30, "source": "manual", "expires_at": "2026-06-25T00:00:00Z"}
  ]
}
```

Expired entries are filtered automatically. The legacy `--no-adjustments`
flag on `scripts/03_simulate.py` zeros the manual overlay; the API feed is
controlled by whether `API_FOOTBALL_KEY` is set in CI.

## Project layout

```
fifa-wc-26-prediction/
├── data/
│   ├── raw/
│   │   ├── results.csv                            # 49k matches since 1872
│   │   ├── wc2026_config.json                     # groups, schedule, venues, FIFA pts
│   │   ├── knockout_bracket_2026.json             # FIFA R32 → final structure
│   │   ├── annex_c_third_place_table_2026.json    # 495-row Annex C lookup
│   │   ├── tiebreakers_2026.json                  # FIFA 2026 tiebreaker rules
│   │   ├── squad_values_2026.json                 # Transfermarkt per team
│   │   ├── host_city_distance_matrix.json         # pre-computed 16×16 km matrix
│   │   └── host_city_distances.py                 # one-shot builder
│   ├── processed/                                  # parquet + JSON outputs
│   └── live/
│       ├── results_2026.json                      # completed match scorelines (10-min refresh)
│       ├── team_adjustments.json                  # manual injury overlay (operator-curated)
│       ├── injuries_2026.json                     # API-Football injuries (3h refresh)
│       ├── weather_2026.json                      # Open-Meteo forecast per venue
│       ├── lineups_2026.json                      # API-Football confirmed XI (T-4h window)
│       ├── match_stats_2026.json                  # post-match stats proxy (NOT xG)
│       ├── matchday_intelligence_log.jsonl        # append-only audit log
│       ├── live_team_state.json                   # mid-tournament soft Elo deltas
│       └── provider_fixture_map.json              # provider fixture_id → our match_id
├── scripts/
│   ├── 01_prepare_data.py … 09_validate.py        # main pipeline
│   ├── live/
│   │   ├── run_live_update.py                     # 10-min orchestrator (fast workflow)
│   │   ├── fetch_results.py                       # API-Football / football-data results
│   │   ├── fetch_injuries.py                      # API-Football /injuries  (B.3)
│   │   ├── fetch_weather.py                       # Open-Meteo forecast    (B.2)
│   │   ├── fetch_lineups.py                       # API-Football /lineups  (B.4)
│   │   ├── fetch_match_stats.py                   # API-Football /statistics (B.5, post-match)
│   │   ├── apply_matchday_adjustments.py          # consolidator + audit log (B.1)
│   │   ├── injury_adjustments.py                  # tier helpers
│   │   ├── weather_adjustments.py                 # heat-index / wet-bulb / bucket classifier
│   │   ├── lineup_adjustments.py                  # GK-swap / rotation heuristics
│   │   ├── stats_proxy_adjustments.py             # shots / possession / corners (NOT xG)
│   │   ├── build_provider_fixture_map.py          # one-shot fixture-id mapper
│   │   └── update_team_state.py                   # soft Elo delta from completed results
│   ├── research/
│   │   ├── probe_apifootball_knockouts.py         # one-shot knockout-shape probe
│   │   └── cwc2025_weather_calibration.py        # CWC2025 backtest of weather table (B.6)
│   └── pre_flight.py                              # 178-check launch audit
├── models/                                         # trained joblib + metrics JSON
├── dashboard/
│   ├── index.html                                  # main dashboard
│   ├── methodology.html                            # plain-English walkthrough
│   ├── appendix.html                               # downloads + known limitations
│   ├── app.js  +  styles.css  +  methodology.css  +  appendix.css
│   └── *.json                                      # all data files served statically
├── vercel.json                                     # static-host config
├── .github/workflows/
│   ├── daily-baseline.yml                          # nightly retrain + deploy
│   ├── live-matchday.yml                           # 10-min results + simulator (fast)
│   ├── matchday-intel-slow.yml                     # 3h injuries/weather/lineups/stats (slow)
│   └── probe-apifootball.yml                       # manual research probe
├── requirements.txt
└── README.md
```

## Model performance (v3)

<!-- AUTO:MODEL_METRICS:BEGIN -->
| Metric                          | Value  | Notes |
|---|---|---|
| Holdout log-loss                | 0.869 | lower is better |
| Holdout Brier                   | 0.511 | lower is better |
| Holdout accuracy                | 60.2% | always-home ≈ 48% |
| WC walk-forward avg log-loss    | 0.983 | mean across 2010/14/18/22 |
| Annex C lookup misses           | 0     | target 0 / 25,000+ sims |
<!-- AUTO:MODEL_METRICS:END -->

> **Honest disclosure on the walk-forward.** The avg-vs-naive lift is +0.10 over 2010/14/18/22,
> but 2022 lift vs the Elo-only baseline collapses to **+0.0085** (essentially zero). The
> goal model holds up at WCs that look like the training distribution and adds little at
> WCs that don't. 2026 may or may not look like 2022 — treat any single-tournament narrative
> with skepticism.

<!-- AUTO:TOP_CONTENDERS:BEGIN -->
## Top contenders (latest run — 25,000 sims, 5 seeds × 5,000)

| # | Team       | Champion | 95% CI       | Reach SF | Model Elo |
|---|---|---|---|---|---|
| 1 | Spain      | 25.5% | [24.9, 26.0] | 49.4% | 2209 |
| 2 | Argentina  | 18.3% | [18.0, 18.6] | 44.0% | 2174 |
| 3 | France     | 9.1% | [8.6, 9.7] | 28.7% | 2116 |
| 4 | England    | 6.6% | [6.3, 6.9] | 23.1% | 2081 |
| 5 | Brazil     | 5.3% | [5.1, 5.7] | 21.5% | 2054 |
| 6 | Colombia   | 4.9% | [4.5, 5.2] | 18.8% | 2049 |
<!-- AUTO:TOP_CONTENDERS:END -->

> "Model Elo" is the in-repo Elo (modified Glicko base + extra friendlies +
> exponential time decay). It runs ~50–100 above public scales like
> eloratings.net by design; rank order is what's meaningful, not absolute values.

Both the metrics and the contenders tables above are regenerated nightly
from `data/processed/predictions.json` by `scripts/10_regen_readme.py` — do
not edit by hand.

## Travel impact (group stage)

| Team        | KM travelled | Champion-prob Δ vs no travel |
|---|---|---|
| Czechia     | 4,544 km     | (mid-pack effect)            |
| South Africa| 3,943 km     | (mid-pack effect)            |
| Canada      | 3,357 km     | (mid-pack effect)            |
| Spain       | ~700 km      | **+1.35pp** (benefits from others' fatigue) |
| France      | varies       | **−0.93pp** (Group I travel) |

## Sensitivity audit (across 22 scenarios)

| Team       | Mean  | Min   | Max   | Range |
|---|---|---|---|---|
| Spain      | 25.0% | 23.2% | 28.2% | 5.0pp |
| Argentina  | 20.2% | 18.6% | 22.0% | 3.3pp |
| France     | 8.8%  | 8.0%  | 10.3% | 2.3pp |
| England    | 6.5%  | 6.0%  | 6.9%  | 0.9pp |

Top-6 rank ordering identical across all 22 scenarios. The model is robust.

## Known limitations

- **True per-shot xG**: deferred — international xG data is patchy pre-2017 and the per-shot location stream isn't in any provider's free tier. The post-match stats proxy uses shots-on-target + possession + corner deltas (capped ±8/match, ±20/group) and is **deliberately not labelled xG** (`true_xg_available` is hard-coded `false`).
- **Injury importance is API-default, not per-player**: API-Football `/injuries` doesn't expose player rating, so the auto-feed assigns tier-2 starter (-12 Elo) to every missing player. The operator manual overlay in `data/live/team_adjustments.json` stacks on top for tier-1 calls (Mbappé / Bellingham / Rodri-tier).
- **Lineup adjustments are conservative**: heuristic v1 only fires on confirmed GK swap (-8 Elo) or ≥3 outfield changes vs the team's last recorded XI (-3 Elo). First XI of the tournament is display-only (no baseline). Capped ±20.
- **Weather forecast horizon**: 16 days (Open-Meteo). The 40-day tournament outlives that — past the horizon, the static climate bucket carries the load (`mild` / `hot` / `very_hot` / `high_altitude_*`).
- **Pre-tournament Elo baseline**: held static. Matchday intelligence adjusts *effective* Elo per match (via aggregated caps); the underlying historical rating doesn't retrain mid-tournament.
- **Refereeing patterns**: not modeled.
- **News + social signals**: out of scope.

## Data sources

- **Match history**: [martj42/international_results](https://github.com/martj42/international_results) (CC0) — every game since 1872
- **FIFA rankings**: live tracker (June 2026)
- **2026 schedule + bracket**: FIFA.com (final draw 5 Dec 2025), Annex C regulations
- **Squad values**: Transfermarkt via Sportingpedia / GiveMeSport
- **Stadium metadata**: hand-curated host city coordinates, altitude, climate

## License

Code under MIT. Data under their respective licenses.
