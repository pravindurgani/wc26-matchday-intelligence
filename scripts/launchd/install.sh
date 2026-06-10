#!/usr/bin/env bash
# Install the wc26-preview launchd agent — full Choice 3 autopilot for the
# feature-branch preview at wc26-matchday-intelligence.vercel.app. Never
# touches main, never touches the production canonical domain.
#
# Usage:
#   ./scripts/launchd/install.sh        # install + load (starts immediately)
#   ./scripts/launchd/install.sh status # check whether agent is loaded
#
# Once installed, the agent fires every 15 min, but the wrapper exits in <1s
# outside the tournament window so the wake-up cost is trivial. During the
# window it runs scripts/deploy_preview.sh which: fetches results → re-sims
# if needed → validates → vercel deploy (preview only) → re-aliases.

set -euo pipefail

PLIST_NAME="com.prav.wc26-preview"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/${PLIST_NAME}.plist"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_NAME}.plist"

if [[ "${1:-}" == "status" ]]; then
  if launchctl list | grep -q "$PLIST_NAME"; then
    launchctl list "$PLIST_NAME" 2>&1 | head -20
    echo ""
    echo "→ Loaded. Next fire interval: 15 min. Log: logs/launchd-tick.log"
  else
    echo "→ NOT loaded. Run: ./scripts/launchd/install.sh"
  fi
  exit 0
fi

# Defensive: ensure the wrapper is executable + present.
WRAPPER="$(cd "$(dirname "$0")" && pwd)/run_if_tournament.sh"
if [[ ! -x "$WRAPPER" ]]; then
  echo "→ chmod +x $WRAPPER"
  chmod +x "$WRAPPER"
fi
if [[ ! -x "$(cd "$(dirname "$0")"/../.. && pwd)/scripts/deploy_preview.sh" ]]; then
  echo "FATAL: scripts/deploy_preview.sh is not executable. Run: chmod +x scripts/deploy_preview.sh"
  exit 1
fi

# Bootstrap: unload if already loaded (idempotent install).
if launchctl list | grep -q "$PLIST_NAME"; then
  echo "→ Unloading existing agent (clean re-install)"
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi

mkdir -p "$(dirname "$PLIST_DEST")"
cp "$PLIST_SRC" "$PLIST_DEST"
echo "→ Copied plist to $PLIST_DEST"

launchctl load "$PLIST_DEST"
echo "→ Loaded agent: $PLIST_NAME"

echo ""
echo "✅ Choice 3 autopilot is live. The preview at"
echo "   https://wc26-matchday-intelligence.vercel.app/"
echo "   will refresh automatically every 15 min during tournament windows"
echo "   (11 Jun – 19 Jul 2026, 11:00–23:00 UTC, plus 05:00 UTC baseline)."
echo ""
echo "Watch the log:"
echo "   tail -f logs/launchd-tick.log"
echo ""
echo "Disable later with:"
echo "   ./scripts/launchd/uninstall.sh"
