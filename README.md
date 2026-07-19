<div align="center">

# 🏆 World Cup 2026 — Matchday Intelligence

**See which World Cup 2026 teams are overvalued, undervalued, and most affected by live matchday conditions.**

![WC26 Matchday Intelligence](dashboard/og-image.svg)

[![Live Dashboard](https://img.shields.io/badge/live-wc26--matchday--intelligence.vercel.app-000?logo=vercel)](https://wc26-matchday-intelligence.vercel.app/)
[![Model](https://img.shields.io/badge/model-v3.0.0-blue)](https://wc26-matchday-intelligence.vercel.app/methodology.html)
[![License](https://img.shields.io/badge/license-MIT-green)](#license)
[![Test + pre-flight](https://github.com/pravindurgani/wc26-matchday-intelligence/actions/workflows/test.yml/badge.svg)](https://github.com/pravindurgani/wc26-matchday-intelligence/actions/workflows/test.yml)
[![Daily Refresh](https://img.shields.io/badge/refresh-every%2010%20min-orange)](#)

### [→ Open the live dashboard ←](https://wc26-matchday-intelligence.vercel.app/)

</div>

---

## ⚽ What you can do in 30 seconds

<table>
<tr>
<td width="33%" valign="top">

### 📈 Biggest mover today
See which team's championship probability shifted the most since yesterday — and why (injury? lineup? result?).

**→** [Dashboard › Movers](https://wc26-matchday-intelligence.vercel.app/#movers)

</td>
<td width="33%" valign="top">

### ⚽ Most interesting next match
Live form, weather, lineups, and travel fatigue rolled into one storyline per fixture.

**→** [Dashboard › Interesting](https://wc26-matchday-intelligence.vercel.app/#interesting)

</td>
<td width="33%" valign="top">

### 🎯 Live matchday adjustments
Per-team Elo deltas from injuries, weather, lineups, and stats — every tick auditable.

**→** [Dashboard › Matchday Intel](https://wc26-matchday-intelligence.vercel.app/#matchday-intel)

</td>
</tr>
</table>

**Current top contenders →** [live dashboard](https://wc26-matchday-intelligence.vercel.app/) for today's numbers · [snapshot below](#top-contenders) regenerates nightly.

---

## 🚀 Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/03_simulate.py          # ~30s, 25k Monte Carlo sims
cd dashboard && python3 -m http.server 8765      # → http://localhost:8765
```

<details>
<summary><b>Full pipeline rebuild (~5 minutes)</b></summary>

```bash
.venv/bin/python scripts/01_prepare_data.py      # ingest 49k matches
.venv/bin/python scripts/02_goal_model.py        # train XGBoost Poisson
.venv/bin/python scripts/04_evaluate.py          # calibration + holdout
.venv/bin/python scripts/06_ablation.py          # Elo-only vs goal-model
.venv/bin/python scripts/07_walk_forward.py      # WC 2010/14/18/22 backtest
.venv/bin/python scripts/05_sensitivity.py       # 27-scenario audit
.venv/bin/python scripts/03_simulate.py --no-travel --out predictions_no_travel.json
.venv/bin/python scripts/03_simulate.py          # full sim
.venv/bin/python scripts/08_travel_impact.py
.venv/bin/python scripts/09_validate.py          # 212-check launch gate
```

</details>

<details>
<summary><b>Simulator CLI flags</b></summary>

```bash
python scripts/03_simulate.py --quick           # 3 seeds × 2k sims (~10s)
python scripts/03_simulate.py --seeds 5 --sims 5000   # production
python scripts/03_simulate.py --no-travel       # disable travel fatigue
python scripts/03_simulate.py --no-dispersion   # Poisson instead of NB
python scripts/03_simulate.py --no-adjustments  # ignore injury overlay
python scripts/03_simulate.py --live            # use locked match results
```

</details>

---

## 📦 What's in the box

| Script | Role |
|---|---|
| `scripts/01_prepare_data.py` | Ingest 49k internationals, normalize names, compute Elo |
| `scripts/02_goal_model.py` | Train XGBoost Poisson regressors (home/away goals) |
| `scripts/03_simulate.py` | **Monte Carlo sim** — Annex C bracket, NB+Dixon-Coles, travel, injuries, live mode |
| `scripts/04_evaluate.py` | Calibration + holdout backtest |
| `scripts/05_sensitivity.py` | 27-scenario sensitivity audit |
| `scripts/07_walk_forward.py` | Walk-forward backtest on WC 2010/14/18/22 |
| `scripts/09_validate.py` | Pre-launch validator (versions, sims, JSON, secrets) |
| `scripts/live/*` | Matchday intel: injuries, weather, lineups, stats, suspensions, referees |

Full project layout lives under `scripts/` and `data/` — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) if present, otherwise browse the tree.

---

## 🛰️ Live matchday intelligence

Five fetchers fan out on a 3-hour cron and consolidate into a single per-team Elo adjustment that the simulator picks up via `apply_matchday_adjustments.get_team_elo_adjustment()` ([`scripts/live/apply_matchday_adjustments.py`](scripts/live/apply_matchday_adjustments.py)):

| Layer        | Source                                  | Cap (Elo)            |
|--------------|-----------------------------------------|----------------------|
| Injuries     | API-Football `/injuries`                | ±25 / extreme ±35    |
| Weather      | Open-Meteo (16-day + climate fallback)  | ±15                  |
| Lineups      | API-Football `/fixtures/lineups`        | ±20                  |
| Stats proxy  | API-Football `/fixtures/statistics`     | ±8/match, ±20/group  |
| Suspensions  | derived from cards in `/fixtures/events`| ±12 / per starter    |
| **Aggregate**| sum across layers (hard cap)            | **±35 / team / match** |
| **Grand total** | + mid-tournament live form delta     | **±45**              |

Every decision is appended to `data/live/matchday_intelligence_log.jsonl` so any probability move is traceable to its source row.

---

## 📊 Model performance (v3)

<!-- AUTO:MODEL_METRICS:BEGIN -->
> _Snapshot: 2026-07-19 07:44:33 UTC · regenerates nightly · [live dashboard](https://wc26-matchday-intelligence.vercel.app/) for current numbers._

| Metric                          | Value  | Notes |
|---|---|---|
| Holdout log-loss                | 0.869 | lower is better |
| Holdout Brier                   | 0.511 | lower is better |
| Holdout accuracy                | 60.3% | always-home ≈ 48% |
| WC walk-forward avg log-loss    | 0.985 | mean across 2010/14/18/22 |
| Annex C lookup misses           | 0     | target 0 / 25,000+ sims |
<!-- AUTO:MODEL_METRICS:END -->

> **Honest disclosure.** Walk-forward avg lift is +0.10 over 2010/14/18/22, but 2022 lift vs Elo-only collapses to **+0.0085**. The goal model holds up when a WC looks like the training distribution and adds little when it doesn't — treat any single-tournament narrative with skepticism.

<a id="top-contenders"></a>
<!-- AUTO:TOP_CONTENDERS:BEGIN -->
## Top contenders (latest run — 25,000 sims, 5 seeds × 5,000)

> _Snapshot: 2026-07-19 07:44:33 UTC · regenerates nightly · [live dashboard](https://wc26-matchday-intelligence.vercel.app/) for current numbers._

| # | Team       | Champion | Sim range (5 seeds) | Reach SF | Model Elo |
|---|---|---|---|---|---|
| 1 | Spain      | 26.0% | [25.5, 26.8] | 52.1% | 2209 |
| 2 | Argentina  | 21.2% | [20.8, 21.4] | 47.9% | 2174 |
| 3 | France     | 10.4% | [10.2, 10.6] | 30.7% | 2116 |
| 4 | England    | 6.0% | [5.7, 6.4] | 22.3% | 2081 |
| 5 | Colombia   | 5.0% | [4.9, 5.3] | 19.3% | 2049 |
| 6 | Brazil     | 4.0% | [3.7, 4.4] | 19.6% | 2054 |
<!-- AUTO:TOP_CONTENDERS:END -->

> "Model Elo" is in-repo (modified Glicko + extra friendlies + exponential time decay). Runs ~50–100 above eloratings.net by design — rank order is what's meaningful. Tables above are regenerated nightly by `scripts/10_regen_readme.py`; do not edit by hand.

---

## ⚠️ Known limitations (the honest list)

- **True per-shot xG** — deferred. International xG is patchy pre-2017 and no provider exposes shot locations on a free tier. Post-match stats proxy uses shots-on-target + possession + corner deltas and is **deliberately not labelled xG** (`true_xg_available = false`).
- **Injury importance is API-default, not per-player** — API-Football doesn't expose player rating. Auto-feed assigns tier-2 starter (-12 Elo) to every missing player; the operator overlay in `data/live/team_adjustments.json` stacks for tier-1 calls (Mbappé / Bellingham / Rodri-tier).
- **Lineup heuristic is conservative** — fires only on confirmed GK swap (-8) or ≥3 outfield changes vs the team's last recorded XI (-3). First XI of the tournament is display-only.
- **Weather horizon = 16 days** — the 40-day tournament outlives it. Past horizon, the static climate bucket carries the load.
- **Pre-tournament Elo is static** — matchday intel adjusts *effective* Elo per match; historical ratings don't retrain mid-tournament.
- **Refereeing patterns + social signals** — out of scope.

---

## 📚 Data sources

- **Match history** — [martj42/international_results](https://github.com/martj42/international_results) (CC0), every game since 1872
- **FIFA rankings** — live tracker (June 2026)
- **2026 schedule + bracket** — FIFA.com (final draw 5 Dec 2025), Annex C regulations
- **Squad values** — Transfermarkt via Sportingpedia / GiveMeSport
- **Stadium metadata** — hand-curated host city coordinates, altitude, climate
- **Live feeds** — API-Football (injuries, lineups, stats, events), Open-Meteo (weather)

---

## License

Code under **MIT**. Data under their respective licenses. Not affiliated with FIFA.
