#!/usr/bin/env bash
# Manual preview-deploy helper for the personal-use Vercel link
# (wc26-matchday-intelligence.vercel.app). NEVER touches main.
#
# Usage:
#   ./scripts/deploy_preview.sh                  # full live refresh + deploy
#   ./scripts/deploy_preview.sh --dry-run        # build only, no deploy
#   ./scripts/deploy_preview.sh --skip-sim       # deploy dashboard as-is
#
# What it does:
#   1. Runs the live orchestrator (fetch_results → re-sim if changed)
#   2. Copies fresh JSON to dashboard/
#   3. Validates (09_validate.py — 38/38 expected)
#   4. `vercel deploy` (preview, NOT --prod)
#   5. Re-aliases to wc26-matchday-intelligence.vercel.app
#
# Pre-reqs: vercel CLI logged in (`vercel login`), .vercel/ linked at repo root,
# .venv/bin/python available at repo root.

set -euo pipefail

cd "$(dirname "$0")/.."   # ensure we're at repo root
REPO="$(pwd)"
PY="${REPO}/.venv/bin/python"
DRY_RUN=0
SKIP_SIM=0
ALIAS_NAME="wc26-matchday-intelligence.vercel.app"

for arg in "$@"; do
  case "$arg" in
    --dry-run)  DRY_RUN=1  ;;
    --skip-sim) SKIP_SIM=1 ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *)
      echo "Unknown flag: $arg" >&2; exit 1 ;;
  esac
done

if [ ! -x "$PY" ]; then
  echo "FATAL: $PY not found. Activate or create .venv first." >&2
  exit 1
fi

echo "── 1/5 · Live orchestrator (fetch results + re-sim if needed) ──"
if [ "$SKIP_SIM" = "0" ]; then
  "$PY" scripts/live/run_live_update.py || {
    echo "  WARN: orchestrator returned non-zero. Continuing with whatever the previous tick left in place." >&2
  }
else
  echo "  skipped (--skip-sim)"
fi

echo ""
echo "── 2/5 · Sync fresh JSON to dashboard/ ──"
for f in predictions predictions_live calibration travel_impact; do
  if [ -f "data/processed/${f}.json" ]; then
    cp "data/processed/${f}.json" "dashboard/${f}.json"
    echo "  ✓ dashboard/${f}.json"
  fi
done
for f in walk_forward ablation sensitivity; do
  if [ -f "models/${f}.json" ]; then
    cp "models/${f}.json" "dashboard/${f}.json"
    echo "  ✓ dashboard/${f}.json"
  fi
done

echo ""
echo "── 3/5 · Validate (38/38 required) ──"
"$PY" scripts/09_validate.py | tail -6

echo ""
echo "── 4/5 · Vercel deploy (PREVIEW only — never touches production) ──"
if [ "$DRY_RUN" = "1" ]; then
  echo "  --dry-run: would run 'vercel deploy' here"
  exit 0
fi
DEPLOY_OUT="$(npx --yes vercel@latest deploy --yes 2>&1)"
echo "$DEPLOY_OUT"
PREVIEW_URL="$(echo "$DEPLOY_OUT" | grep -E '^\s*https://.*\.vercel\.app' | tail -1 | tr -d ' ')"
if [ -z "$PREVIEW_URL" ]; then
  echo "FATAL: couldn't parse preview URL from vercel output. Did the deploy fail?" >&2
  exit 1
fi
echo ""
echo "  Preview URL: $PREVIEW_URL"

echo ""
echo "── 5/5 · Re-alias preview to ${ALIAS_NAME} ──"
npx --yes vercel@latest alias set "$PREVIEW_URL" "$ALIAS_NAME"
echo ""
echo "✅ DONE. Open: https://${ALIAS_NAME}"
echo ""
echo "Reminder: this script never touches main."
