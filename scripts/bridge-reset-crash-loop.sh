#!/usr/bin/env bash
# Clear the deliberate crash-loop hold and restart the selected bridge service.
set -euo pipefail

if [ -n "${POCKET_BRIDGE_LABEL:-}" ]; then
  LABEL="$POCKET_BRIDGE_LABEL"
elif command -v systemctl >/dev/null 2>&1; then
  LABEL="pocket-bridge"
elif [ -f "$HOME/Library/LaunchAgents/com.pocketconnect.bridge.plist" ]; then
  LABEL="com.pocketconnect.bridge"
elif [ -f "$HOME/Library/LaunchAgents/ai.studio.hermes-bridge.plist" ]; then
  LABEL="ai.studio.hermes-bridge"
else
  LABEL="pocket-bridge"
fi

if [ -n "${POCKET_BRIDGE_CRASH_STATE_DIR:-}" ]; then
  STATE_DIR="$POCKET_BRIDGE_CRASH_STATE_DIR"
elif [ "$(uname -s)" = "Darwin" ]; then
  STATE_DIR="$HOME/Library/Application Support/PocketConnect/bridge-state"
else
  STATE_DIR="$HOME/.local/state/pocket-lobster/bridge"
fi

rm -f "$STATE_DIR/crash-loop.hold" "$STATE_DIR/failures"

if command -v systemctl >/dev/null 2>&1 && systemctl --user show "$LABEL.service" >/dev/null 2>&1; then
  systemctl --user reset-failed "$LABEL.service" || true
  systemctl --user restart "$LABEL.service"
  printf 'crash-loop guard reset and %s restarted\n' "$LABEL"
  exit 0
fi

if command -v launchctl >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  printf 'crash-loop guard reset and %s kickstarted\n' "$LABEL"
  exit 0
fi

printf 'no supported service manager found for %s\n' "$LABEL" >&2
exit 1
