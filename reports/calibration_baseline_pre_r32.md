# Calibration baseline — pre-R32

**Date:** 2026-06-17
**Probe:** `scripts/calibration.py`
**Inputs:**
- `data/processed/predictions_live.json`
- `data/live/results_2026.json`
- `--max-m 72` (group stage)

## Headline

| metric | value | baseline | lift |
|---|---|---|---|
| N completed fixtures | **5** | — | — |
| Model log loss | **1.0352** | uniform-1/3 = 1.0986; long-run (0.45/0.27/0.28) = 1.0028 | vs uniform **−0.0634** (better); vs long-run **+0.0324** (worse) |
| Model Brier (multi-class) | **0.6763** | uniform = 0.6667; long-run = 0.5978 | vs uniform **+0.0096** (worse); vs long-run **+0.0785** (worse) |
| Calibration verdict | **MIXED** | — | — |
| Matched fixtures (m) | 1, 2, 3, 4, 5 | — | — |
| Class mix | home=3, draw=2, away=0 | — | — |

## Top-line verdict

**N is far too small to displace the pre-tournament backtest signal.**
Pre-tournament CWC2025 backtest log loss was **0.957**, and that remains
the dominant calibration signal until **N ≥ 30** completed fixtures.
At N=5, the in-tournament log loss of 1.0352 is dominated by sampling
noise (a single mis-priced fixture moves the mean by ~0.2). The
in-tournament number is recorded here for the historical record and as
a regression marker — it is **not** evidence the model has drifted.

The MIXED verdict is expected at this sample size; reliability buckets
are sparsely populated (most have n=0 or n=1).

## Reliability table

Bins are equal-width `[i/10, (i+1)/10)` on predicted probability of the
positive class; final bin is closed on the right.

### HOME (positive class = home win)

| bin_lo | bin_hi | n | mean_pred | actual_freq |
|---|---|---|---|---|
| 0.00 | 0.10 | 1 | 0.076 | 0.000 |
| 0.10 | 0.20 | 0 | 0.000 | 0.000 |
| 0.20 | 0.30 | 0 | 0.000 | 0.000 |
| 0.30 | 0.40 | 0 | 0.000 | 0.000 |
| 0.40 | 0.50 | 2 | 0.432 | 1.000 |
| 0.50 | 0.60 | 0 | 0.000 | 0.000 |
| 0.60 | 0.70 | 1 | 0.690 | 0.000 |
| 0.70 | 0.80 | 1 | 0.703 | 1.000 |
| 0.80 | 0.90 | 0 | 0.000 | 0.000 |
| 0.90 | 1.00 | 0 | 0.000 | 0.000 |

### DRAW (positive class = draw)

| bin_lo | bin_hi | n | mean_pred | actual_freq |
|---|---|---|---|---|
| 0.00 | 0.10 | 0 | 0.000 | 0.000 |
| 0.10 | 0.20 | 1 | 0.198 | 1.000 |
| 0.20 | 0.30 | 4 | 0.252 | 0.250 |
| 0.30 | 0.40 | 0 | 0.000 | 0.000 |
| 0.40 | 0.50 | 0 | 0.000 | 0.000 |
| 0.50 | 0.60 | 0 | 0.000 | 0.000 |
| 0.60 | 0.70 | 0 | 0.000 | 0.000 |
| 0.70 | 0.80 | 0 | 0.000 | 0.000 |
| 0.80 | 0.90 | 0 | 0.000 | 0.000 |
| 0.90 | 1.00 | 0 | 0.000 | 0.000 |

### AWAY (positive class = away win)

| bin_lo | bin_hi | n | mean_pred | actual_freq |
|---|---|---|---|---|
| 0.00 | 0.10 | 2 | 0.089 | 0.000 |
| 0.10 | 0.20 | 0 | 0.000 | 0.000 |
| 0.20 | 0.30 | 2 | 0.278 | 0.000 |
| 0.30 | 0.40 | 0 | 0.000 | 0.000 |
| 0.40 | 0.50 | 0 | 0.000 | 0.000 |
| 0.50 | 0.60 | 0 | 0.000 | 0.000 |
| 0.60 | 0.70 | 0 | 0.000 | 0.000 |
| 0.70 | 0.80 | 1 | 0.726 | 0.000 |
| 0.80 | 0.90 | 0 | 0.000 | 0.000 |
| 0.90 | 1.00 | 0 | 0.000 | 0.000 |

## Reproducibility

```
python3 scripts/calibration.py \
    --predictions data/processed/predictions_live.json \
    --results data/live/results_2026.json \
    --json
```

JSON output captured 2026-06-17. Inputs were last touched
`predictions_live.json` 2026-06-12 00:07 and `results_2026.json`
2026-06-16 22:12 (file mtimes). Re-running with later result snapshots
will produce different numbers as N grows; this file is the snapshot for
**pre-R32** decision-making only.

## Re-validation plan

- At N ≥ 30 (mid-group-stage onward), the in-tournament log loss
  starts to dominate the CWC2025 backtest signal and this file
  should be superseded by a fresh probe.
- If in-tournament log loss crosses **1.05** (the
  `BROKEN_LOG_LOSS` threshold in `scripts/calibration.py:52`), the
  probe exits with code 1 — that is the gating signal, not this file.
