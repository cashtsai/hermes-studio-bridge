"""Pure capability-aware placement planner for T3Code multi-host routing.

This module intentionally plans only.  It never moves a live provider session
and never handles credentials.  A caller must provide a durable checkpoint and
an explicit failover decision before applying a plan to a running turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


ACTIVE_SESSION_STATES = frozenset({"running", "streaming", "waiting_user", "queued"})


@dataclass(frozen=True)
class Host:
    id: str
    capabilities: frozenset[str]
    capacity: int = 1
    load: int = 0
    active: bool = True
    region: str = ""


@dataclass(frozen=True)
class Placement:
    status: str
    host_id: str | None
    reason: str
    candidates: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"status": self.status, "host_id": self.host_id,
                "reason": self.reason, "candidates": list(self.candidates)}


def _host(value: Host | dict) -> Host:
    if isinstance(value, Host):
        return value
    return Host(
        id=str(value.get("id") or "").strip(),
        capabilities=frozenset(str(x) for x in value.get("capabilities") or []),
        capacity=max(1, int(value.get("capacity") or 1)),
        load=max(0, int(value.get("load") or 0)),
        active=value.get("active", True) is not False,
        region=str(value.get("region") or ""),
    )


def plan_placement(hosts: Iterable[Host | dict], *, required: Iterable[str] = (),
                   current_host_id: str | None = None,
                   session_state: str = "idle", checkpointed: bool = False,
                   allow_failover: bool = False) -> Placement:
    """Return a conservative placement decision.

    A live session remains sticky to its current healthy host.  A failed live
    session can only be placed elsewhere when the caller proves a checkpoint
    and explicitly opts into failover.  New work selects the least-loaded host
    that advertises every required capability.
    """
    rows = tuple(h for h in (_host(x) for x in hosts) if h.id)
    required_set = frozenset(str(x) for x in required if str(x))
    by_id = {h.id: h for h in rows}
    current = by_id.get(str(current_host_id or ""))
    if current and current.active:
        # The current turn is part of the host's load; capacity full does not
        # mean the existing session should be evicted or silently moved.
        if session_state in ACTIVE_SESSION_STATES:
            return Placement("sticky", current.id,
                             "active session remains on its current host")
        if current.load >= current.capacity and session_state not in {"failed", "disconnected"}:
            return Placement("queued", None,
                             "current host is at capacity for new work")
        if session_state not in {"failed", "disconnected"}:
            return Placement("sticky", current.id,
                             "existing session keeps its host affinity")

    if session_state in ACTIVE_SESSION_STATES and current_host_id:
        return Placement("blocked", None,
                         "active session cannot move without durable checkpoint")
    if session_state in {"failed", "disconnected"} and not (
            allow_failover and checkpointed):
        return Placement("blocked", None,
                         "failover requires explicit approval and checkpoint")

    candidates = tuple(
        h for h in rows
        if h.active and h.load < h.capacity and required_set.issubset(h.capabilities)
    )
    if not candidates:
        return Placement("queued", None,
                         "no active host has the required capability/capacity")
    ordered = tuple(sorted(candidates,
                           key=lambda h: (h.load / max(h.capacity, 1),
                                           h.id)))
    return Placement("assigned", ordered[0].id,
                     "least-loaded capable host", tuple(h.id for h in ordered))
