# v3.0.0 — World Cup 2026 ready

Released: **2026-06-25** (T-3 days to R32)

## Highlights

- **Full simulation pipeline** — Dixon-Coles + Negative Binomial scoreline
  model with XGBoost Poisson goal regressors, Annex C bracket, full FIFA
  2026 tiebreaker cascade, travel-fatigue and injury layers.
- **Live matchday intelligence** — weather (Open-Meteo), injuries
  (API-Football), lineup heuristics, post-match form delta, all
  audit-logged and capped per match (±8) and per group stage (±20).
- **Betting engine v2.3.13** (Apps Script, separate `wc26-engine-gs/` repo,
  gitignored from this one) — staking, edge gating, and ledger.
- **R32-ready** — survived 14 rounds of pressure-tests and three rounds
  of external expert review; all HIGH/CRITICAL audit findings closed.

## Quick Start

See `README.md` for the full pipeline. Short version:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/01_prepare_data.py
.venv/bin/python scripts/02_goal_model.py
.venv/bin/python scripts/03_simulate.py
.venv/bin/python scripts/09_validate.py   # must pass before publishing
cd dashboard && python3 -m http.server 8765
```

Deploy with `vercel deploy` (or push the `dashboard/` directory to any
static host).

## Live tournament features

- **Matchday intelligence layer** (`scripts/live/`): weather, injuries,
  lineup changes, stats proxy — each with conservative caps and append-only
  audit log under `data/live/audit/`.
- **Results polling** (`scripts/live/fetch_results.py`): API-Football
  primary, football-data.org fallback; knockout-aware fixture map covering
  M73-M104 (R32 through Final); writes FT/AET/PEN sub-scores.
- **Betting engine v2.3.13** — Google Apps Script project tracked separately.
- **Audit log** — every Elo adjustment is timestamped, sourced, and capped;
  reproducible from the on-disk JSON alone.
- **Live polling + Vercel self-deploy** — workflow regenerates `live_state.json`
  on cadence and triggers a Vercel redeploy without manual intervention.

## Known limitations

Same as `README.md` "Known limitations" section: no true per-shot xG, no
per-player injury importance, no refereeing patterns, no news/social
signals. Pre-tournament Elo is static; the live layer adjusts *effective*
Elo only.

## What's next

- **Post-tournament**: walk-forward vs actual WC 2026 outcomes; calibration report; ablation rerun against live data.
- **Centralized constants** (`scripts/constants.py`): single source of truth for `MAX_G`, `DC_RHO`, `NB_ALPHA` (currently shadow-copied across 6 files; works today, but brittle).
- **Performance**: parallelize seed loop (`joblib.Parallel`), CDF+searchsorted sampling, vectorized noise-path matrix construction — net target: 25k sims in ≤5s on M-series.

## Stats

- 78 test files, ~1056 tests (1272 collected, 1272 pass)
- ~37k LOC Python
- 2141 commits since initial launch (2026-06-09)
- 14 R32 pressure-test rounds, 4 external audit rounds — all HIGH/CRITICAL closed
