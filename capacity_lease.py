"""Pure capacity lease contract for T3Code multi-host placement.

The placement planner decides *where* work should go.  This module decides
whether a host has a short-lived reservation for that work.  It is deliberately
in-memory and deterministic: durable persistence, queue delivery and failover
remain an integration-layer responsibility and are not silently implied here.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
import uuid


class CapacityLeaseError(ValueError):
    """Invalid lease input."""


@dataclass(frozen=True)
class CapacityLease:
    lease_id: str
    host_id: str
    session_id: str
    expires_at: float

    def as_dict(self) -> dict:
        return {
            "lease_id": self.lease_id,
            "host_id": self.host_id,
            "session_id": self.session_id,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class LeaseDecision:
    status: str
    lease: CapacityLease | None = None
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "lease": self.lease.as_dict() if self.lease else None,
            "reason": self.reason,
        }


class CapacityLeaseTable:
    """Short-lived, single-process reservations for a host.

    A session may hold at most one lease on a host.  Re-acquiring the same
    session is idempotent and returns the existing lease; a different session
    is rejected once active leases reach the supplied host capacity.
    """

    MIN_TTL_SECONDS = 5.0
    MAX_TTL_SECONDS = 3600.0

    def __init__(self, *, lease_id_factory=None):
        self._leases: dict[str, CapacityLease] = {}
        self._lease_id_factory = lease_id_factory or (lambda: uuid.uuid4().hex)

    def _validate_text(self, value: str, label: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise CapacityLeaseError(f"{label} must be non-empty")
        return clean

    def _active(self, now: float) -> list[CapacityLease]:
        self.reap(now=now)
        return list(self._leases.values())

    def reap(self, *, now: float | None = None) -> tuple[str, ...]:
        current = time.time() if now is None else float(now)
        expired = tuple(
            lease_id for lease_id, lease in self._leases.items()
            if lease.expires_at <= current
        )
        for lease_id in expired:
            self._leases.pop(lease_id, None)
        return expired

    def acquire(self, *, host_id: str, session_id: str, capacity: int,
                ttl_seconds: float = 60.0,
                now: float | None = None) -> LeaseDecision:
        host = self._validate_text(host_id, "host_id")
        session = self._validate_text(session_id, "session_id")
        try:
            cap = int(capacity)
            ttl = float(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise CapacityLeaseError("capacity and ttl_seconds must be numeric") from exc
        if cap < 1:
            raise CapacityLeaseError("capacity must be at least 1")
        if not self.MIN_TTL_SECONDS <= ttl <= self.MAX_TTL_SECONDS:
            raise CapacityLeaseError(
                f"ttl_seconds must be between {self.MIN_TTL_SECONDS:g} and "
                f"{self.MAX_TTL_SECONDS:g}"
            )
        current = time.time() if now is None else float(now)
        active = self._active(current)
        for lease in active:
            if lease.host_id == host and lease.session_id == session:
                return LeaseDecision("held", lease,
                                     "session already holds a host lease")
        used = sum(lease.host_id == host for lease in active)
        if used >= cap:
            return LeaseDecision("at_capacity", None,
                                 "host capacity is already reserved")
        lease = CapacityLease(
            lease_id=str(self._lease_id_factory()),
            host_id=host,
            session_id=session,
            expires_at=current + ttl,
        )
        self._leases[lease.lease_id] = lease
        return LeaseDecision("acquired", lease, "capacity reserved")

    def release(self, lease_id: str) -> bool:
        return self._leases.pop(str(lease_id or ""), None) is not None

    def find(self, *, session_id: str | None = None,
             host_id: str | None = None,
             now: float | None = None) -> tuple[CapacityLease, ...]:
        active = self._active(time.time() if now is None else float(now))
        session = str(session_id or "").strip()
        host = str(host_id or "").strip()
        return tuple(
            lease for lease in active
            if (not session or lease.session_id == session)
            and (not host or lease.host_id == host)
        )

