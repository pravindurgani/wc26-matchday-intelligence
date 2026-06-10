#!/usr/bin/env bash
# Disable + remove the wc26-preview launchd agent.

set -euo pipefail
PLIST_NAME="com.prav.wc26-preview"
PLIST_DEST="${HOME}/Library/LaunchAgents/${PLIST_NAME}.plist"

if [[ -f "$PLIST_DEST" ]]; then
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
  rm "$PLIST_DEST"
  echo "→ Removed $PLIST_DEST"
else
  echo "→ Plist not installed: $PLIST_DEST"
fi

if launchctl list | grep -q "$PLIST_NAME"; then
  launchctl remove "$PLIST_NAME" 2>/dev/null || true
fi

echo "✅ Autopilot disabled. Preview will only refresh when you run"
echo "   ./scripts/deploy_preview.sh manually."
