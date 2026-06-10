#!/usr/bin/env bash
# Wrapper around deploy_preview.sh that exits cleanly outside the tournament
# window. Called by the launchd agent every 15 min, 24/7 — this script is the
# date-gate. Keeps the plist trivially simple instead of expanding ~3000
# StartCalendarInterval entries.
#
# Window: 11 Jun – 19 Jul 2026, 11:00–23:00 UTC (match days only).
# Also runs the daily-baseline retrain once a day at 05:00 UTC, regardless
# of match window.
#
# Logs every fire (including no-op early exits) to logs/launchd-tick.log
# so you can confirm the agent is alive.

set -euo pipefail

REPO_ROOT="/Users/prav/Desktop/personal-projects/fifa-wc-26-prediction"
LOG="${REPO_ROOT}/logs/launchd-tick.log"
mkdir -p "$(dirname "$LOG")"

UTC_DATE=$(date -u +%Y-%m-%d)
UTC_HOUR=$(date -u +%H)            # 00..23
UTC_HHMM=$(date -u +%H:%M)
ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

log() { echo "[$ISO] $*" >> "$LOG"; }

# Hard window: 2026-06-11 through 2026-07-19 (inclusive).
in_tournament=0
if [[ "$UTC_DATE" > "2026-06-10" && "$UTC_DATE" < "2026-07-20" ]]; then
  in_tournament=1
fi

if [[ "$in_tournament" -eq 0 ]]; then
  log "skip — outside tournament window ($UTC_DATE)"
  exit 0
fi

# Match-window: 11:00–23:00 UTC. Outside this window we still want the
# 05:00 UTC daily-baseline tick to fire so the morning numbers refresh.
is_match_window=0
if [[ "$UTC_HOUR" -ge 11 && "$UTC_HOUR" -le 23 ]]; then
  is_match_window=1
fi
is_baseline_window=0
# 05:00 ± 7 min — launchd fires every 15 min so we hit either 04:55 or 05:00.
if [[ "$UTC_HHMM" == "05:00" || "$UTC_HHMM" == "04:55" ]]; then
  is_baseline_window=1
fi

if [[ "$is_match_window" -eq 0 && "$is_baseline_window" -eq 0 ]]; then
  log "skip — outside match (11-23 UTC) and baseline (05:00 UTC) windows ($UTC_HHMM UTC)"
  exit 0
fi

log "fire — running deploy_preview.sh (match=$is_match_window baseline=$is_baseline_window)"

cd "$REPO_ROOT"
# Sourcing nothing — deploy_preview.sh is self-contained. Capture combined
# output so a partial failure shows up in the log without aborting the agent.
if ./scripts/deploy_preview.sh >> "$LOG" 2>&1; then
  log "ok — deploy_preview.sh succeeded"
else
  rc=$?
  log "FAIL — deploy_preview.sh exited $rc (check log for details)"
fi
