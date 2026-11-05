#!/usr/bin/env bash
# Keep a bridge from spinning forever when a release crashes during startup.
# The parent service stays up in a deliberate hold state after repeated failures;
# an operator or the update transaction must clear the hold before retrying.
set -u

if [ "$#" -eq 0 ]; then
  printf 'usage: %s COMMAND [ARG...]\n' "$0" >&2
  exit 64
fi

STATE_DIR="${POCKET_BRIDGE_CRASH_STATE_DIR:-${TMPDIR:-/tmp}/pocket-bridge-crash-loop}"
LOG_FILE="${POCKET_BRIDGE_CRASH_LOG:-$STATE_DIR/crash-loop.log}"
WINDOW="${POCKET_BRIDGE_CRASH_WINDOW_SEC:-300}"
MAX_FAILURES="${POCKET_BRIDGE_CRASH_MAX:-5}"
RESTART_DELAY="${POCKET_BRIDGE_CRASH_RESTART_SEC:-3}"
FAILURES_FILE="$STATE_DIR/failures"
HOLD_FILE="$STATE_DIR/crash-loop.hold"

mkdir -p "$STATE_DIR"

log() {
  printf '%s [bridge-supervisor] %s\n' "$(date '+%F %T')" "$*" >> "$LOG_FILE"
}

prune_failures() {
  local now cutoff tmp
  now="$(date +%s)"
  cutoff=$((now - WINDOW))
  tmp="$FAILURES_FILE.$$"
  awk -v cutoff="$cutoff" '$1 >= cutoff { print $1 }' "$FAILURES_FILE" \
    > "$tmp" 2>/dev/null || :
  mv -f "$tmp" "$FAILURES_FILE" 2>/dev/null || :
}

hold_until_reset() {
  log "crash-loop breaker open; waiting for reset file removal: $HOLD_FILE"
  while [ -e "$HOLD_FILE" ] && [ "$stopping" -eq 0 ]; do
    # Short sleeps let TERM from launchd/systemd interrupt a held supervisor
    # promptly without polling the filesystem in a tight loop.
    sleep 1
  done
  [ "$stopping" -eq 0 ] || exit 143
  : > "$FAILURES_FILE"
  log "crash-loop breaker reset; retrying"
}

stopping=0
child=""
stop_child() {
  stopping=1
  if [ -n "$child" ]; then
    kill -TERM "$child" 2>/dev/null || :
  fi
}
trap stop_child TERM INT HUP

while :; do
  if [ -e "$HOLD_FILE" ]; then
    hold_until_reset
  fi

  "$@" &
  child=$!
  wait "$child"
  rc=$?
  child=""

  # A service stop/restart is an operator action, not a crash. Do not relaunch
  # the child after launchd/systemd sent the supervisor a termination signal.
  if [ "$stopping" -ne 0 ]; then
    exit 143
  fi
  if [ "$rc" -eq 0 ]; then
    exit 0
  fi

  now="$(date +%s)"
  printf '%s\n' "$now" >> "$FAILURES_FILE"
  prune_failures
  failures="$(wc -l < "$FAILURES_FILE" | tr -d ' ')"
  log "child exited rc=$rc ($failures/$MAX_FAILURES failures in ${WINDOW}s)"

  if [ "$failures" -ge "$MAX_FAILURES" ]; then
    : > "$HOLD_FILE"
    hold_until_reset
  else
    sleep "$RESTART_DELAY"
  fi
done
