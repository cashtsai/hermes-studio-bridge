#!/usr/bin/env bash
# Explicit operator recovery for a snapshot created by lobster-update.sh.
# This is intentionally opt-in: restoring a snapshot can discard writes made
# after the snapshot, so normal code rollback never calls this automatically.
set -euo pipefail

BASE="${POCKET_LOBSTER_BASE:-$HOME/.local/share/pocket-lobster/bridge}"
SNAPSHOTS="$BASE/snapshots"
PREFIX="${1:-}"
LABEL="${POCKET_BRIDGE_LABEL:-pocket-bridge}"
CONFIRM="${CONFIRM_RESTORE:-0}"
CANON="${POCKET_CANON_DB:-$HOME/.local/share/pocket-agent/canonical.db}"
ACCOUNTS="${POCKET_ACCOUNTS_DB:-$HOME/.local/share/pocket-agent/accounts.db}"

if [ -z "$PREFIX" ] || [ "$CONFIRM" != "1" ]; then
  printf 'usage: CONFIRM_RESTORE=1 %s SNAPSHOT_PREFIX\n' "$0" >&2
  printf 'available snapshots:\n' >&2
  find "$SNAPSHOTS" -maxdepth 1 -type f -name '*-canonical.db' -print 2>/dev/null \
    | sed "s#^$SNAPSHOTS/##; s/-canonical\\.db$//" | sort >&2
  exit 64
fi

restore_one() {
  local name="$1" destination="$2" source="$SNAPSHOTS/$PREFIX-$name.db"
  [ -f "$source" ] || { printf 'missing snapshot: %s\n' "$source" >&2; exit 1; }
  mkdir -p "$(dirname "$destination")"
  cp "$source" "$destination"
  chmod 600 "$destination" 2>/dev/null || true
  printf 'restored %s\n' "$name"
}

if command -v systemctl >/dev/null 2>&1 && systemctl --user show "$LABEL.service" >/dev/null 2>&1; then
  systemctl --user stop "$LABEL.service" || true
  restore_one canonical "$CANON"
  restore_one accounts "$ACCOUNTS"
  systemctl --user reset-failed "$LABEL.service" || true
  systemctl --user start "$LABEL.service"
else
  printf 'expected systemd user unit %s.service; stop the bridge manually before restoring\n' "$LABEL" >&2
  exit 1
fi

printf 'SQLite snapshot %s restored; run post-deploy smoke before accepting traffic\n' "$PREFIX"
