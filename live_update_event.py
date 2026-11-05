"""Provider-neutral live update events for iOS and Android clients.

The APNs payload keeps its existing iOS fields, but carries this additive
envelope so Android Live Updates and future transports consume the same state
semantics.  This module is deliberately pure: it does not read credentials,
network state, or provider-specific objects.
"""

from __future__ import annotations

import time
from typing import Any


VERSION = 1
STATES = frozenset({
    "connecting", "queued", "running", "waiting_user", "streaming",
    "completed", "failed", "disconnected", "idle",
})
TERMINAL_STATES = frozenset({"completed", "failed"})
ACTIVE_STATES = frozenset({"queued", "running", "waiting_user", "streaming"})


class LiveUpdateEventError(ValueError):
    """The event cannot be safely consumed by a live-update client."""


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiveUpdateEventError(f"{field} must be non-empty text")
    value = value.strip()
    if len(value) > limit:
        raise LiveUpdateEventError(f"{field} exceeds {limit} characters")
    return value


def make_event(session_id: str, seq: int, state: str, *,
               phase: str | None = None,
               updated_at: float | None = None,
               terminal: bool | None = None) -> dict:
    """Build and validate the shared event envelope.

    Terminal is derived from state unless explicitly supplied.  Explicitly
    marking a non-terminal state as terminal is rejected instead of allowing a
    client to close an activity prematurely.
    """
    session_id = _text(session_id, "session_id", 240)
    try:
        seq = int(seq)
    except (TypeError, ValueError) as exc:
        raise LiveUpdateEventError("seq must be a positive integer") from exc
    if seq <= 0:
        raise LiveUpdateEventError("seq must be a positive integer")
    state = _text(state, "state", 40).lower()
    if state not in STATES:
        raise LiveUpdateEventError(f"unsupported state: {state}")
    if terminal is None:
        terminal = state in TERMINAL_STATES
    if bool(terminal) != (state in TERMINAL_STATES):
        raise LiveUpdateEventError("terminal does not match state")
    if updated_at is None:
        updated_at = time.time()
    try:
        updated_at = float(updated_at)
    except (TypeError, ValueError) as exc:
        raise LiveUpdateEventError("updated_at must be numeric") from exc
    if updated_at <= 0:
        raise LiveUpdateEventError("updated_at must be positive")
    event = {
        "version": VERSION,
        "session_id": session_id,
        "seq": seq,
        "state": state,
        "updated_at": updated_at,
        "terminal": bool(terminal),
    }
    if phase is not None:
        event["phase"] = _text(phase, "phase", 240)
    return event


def next_seq(session_id: str, floor: int = 0) -> int:
    """Return a process-local monotonic sequence safe across restarts.

    The durable event-log cursor should be passed as ``floor`` when available.
    The millisecond clock prevents a fresh process from reusing a small cursor
    when no durable event exists yet; the in-memory map handles same-ms calls.
    """
    current = max(int(floor or 0), int(time.time() * 1000))
    previous = _LAST_SEQ.get(session_id, 0)
    current = max(current, previous + 1)
    _LAST_SEQ[session_id] = current
    return current


_LAST_SEQ: dict[str, int] = {}


def apns_content_state(event: dict, *, busy: bool,
                       phase_label: str | None = None,
                       ended_label: str | None = None) -> dict:
    """Add the shared envelope to the existing ActivityKit state shape."""
    if not isinstance(event, dict):
        raise LiveUpdateEventError("event must be an object")
    if int(event.get("version", 0)) != VERSION:
        raise LiveUpdateEventError("unsupported event version")
    state = event.get("state")
    if state not in STATES:
        raise LiveUpdateEventError("event has unsupported state")
    return {
        "busy": bool(busy),
        "phaseLabel": phase_label,
        "endedLabel": ended_label,
        "version": VERSION,
        "sessionID": event["session_id"],
        "seq": event["seq"],
        "state": state,
        "terminal": bool(event["terminal"]),
    }
