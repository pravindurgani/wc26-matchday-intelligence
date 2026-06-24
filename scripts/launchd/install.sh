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

# Audit H1 (R2 round 3): plist is a template. Substitute __REPO_ROOT__
# with the actual repository root so the agent runs from this checkout,
# not whatever path was hardcoded the day the template was committed.
#
# Pre-flight: refuse to install a template with no substitution markers
# (catches a partial revert that re-introduces hardcoded paths) or with
# unresolved markers post-substitution (catches a sed-failure).
REPO_ROOT_RESOLVED="$(cd "$(dirname "$0")"/../.. && pwd)"

if ! grep -q "__REPO_ROOT__" "$PLIST_SRC"; then
  echo "FATAL: $PLIST_SRC has no __REPO_ROOT__ markers — refusing to install."
  echo "       Either the template was reverted to a hardcoded plist, or"
  echo "       you edited it manually. See com.prav.wc26-preview.plist for"
  echo "       the template format."
  exit 1
fi

# `|` separator avoids needing to escape forward slashes in REPO_ROOT.
sed "s|__REPO_ROOT__|$REPO_ROOT_RESOLVED|g" "$PLIST_SRC" > "$PLIST_DEST"

if grep -q "__REPO_ROOT__" "$PLIST_DEST"; then
  echo "FATAL: substitution left unresolved __REPO_ROOT__ markers in $PLIST_DEST."
  echo "       Check 'sed' availability and the contents of $PLIST_SRC."
  rm -f "$PLIST_DEST"
  exit 1
fi

echo "→ Materialized plist into $PLIST_DEST (REPO_ROOT=$REPO_ROOT_RESOLVED)"

launchctl load "$PLIST_DEST"
echo "→ Loaded agent: $PLIST_NAME"

# TCC probe: macOS blocks launchd-spawned bash from reading files under
# ~/Desktop, ~/Documents, ~/Downloads etc. unless Full Disk Access is
# granted. Detect this NOW (instead of silently failing every 15 min for
# the rest of the tournament) by spawning a one-shot probe agent.
REPO_ROOT="$(cd "$(dirname "$0")"/../.. && pwd)"
case "$REPO_ROOT" in
  "$HOME"/Desktop/*|"$HOME"/Documents/*|"$HOME"/Downloads/*)
    PROBE_PLIST="/tmp/wc26-tcc-probe.plist"
    PROBE_OUT="/tmp/wc26-tcc-probe.out"
    cat > "$PROBE_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.prav.wc26-tcc-probe</string>
<key>ProgramArguments</key><array>
  <string>/bin/bash</string><string>-c</string>
  <string>ls "$REPO_ROOT/scripts/launchd" >/dev/null 2>&1 && echo OK || echo BLOCKED</string>
</array>
<key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>$PROBE_OUT</string>
</dict></plist>
EOF
    rm -f "$PROBE_OUT"
    launchctl load "$PROBE_PLIST" 2>/dev/null
    sleep 2
    launchctl unload "$PROBE_PLIST" 2>/dev/null
    PROBE_RESULT="$(cat "$PROBE_OUT" 2>/dev/null || echo BLOCKED)"
    rm -f "$PROBE_PLIST" "$PROBE_OUT"
    if [[ "$PROBE_RESULT" != "OK" ]]; then
      echo ""
      echo "⚠️  WARNING — macOS TCC blocks launchd from reading $REPO_ROOT"
      echo ""
      echo "Your project is under a protected folder (~/Desktop, ~/Documents,"
      echo "or ~/Downloads). launchd-spawned bash cannot read files there"
      echo "unless Full Disk Access is granted to /bin/bash."
      echo ""
      echo "Two fixes:"
      echo "  (a) System Settings → Privacy & Security → Full Disk Access"
      echo "      → + → Cmd+Shift+G → /bin → select 'bash' → toggle ON,"
      echo "      then re-run this install script."
      echo "  (b) Move the project out: mv \"$REPO_ROOT\" ~/projects/"
      echo "      then re-link Vercel and re-install."
      echo ""
      echo "The agent is loaded but every tick will silently fail until you"
      echo "fix this. Check 'logs/launchd-stderr.log' to confirm."
    else
      echo "→ TCC probe passed: launchd can read $REPO_ROOT"
    fi
    ;;
  *)
    echo "→ Project lives outside ~/Desktop|Documents|Downloads — no TCC concern."
    ;;
esac

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
