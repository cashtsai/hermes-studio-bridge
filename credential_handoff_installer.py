"""Approval-bound target-side credential installation.

The authenticated bridge consumes the metadata-only handoff first.  This
module is the local target boundary after that consume: it requires an
explicit approval id, verifies the consumed ticket binding, and writes the
secret directly to the injected local store.  It never returns or logs the
secret.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from credential_store import CredentialRef, CredentialStoreError


class CredentialHandoffInstallError(RuntimeError):
    """A handoff cannot be installed without exposing its secret."""


class CredentialStore(Protocol):
    def write(self, ref: CredentialRef, secret: str) -> None: ...


def _required(value: object, field: str, limit: int = 240) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or any(ord(ch) < 0x20 for ch in text):
        raise CredentialHandoffInstallError(f"{field} is invalid")
    return text


@dataclass(frozen=True)
class ApprovedCredentialInstaller:
    """Install only a consumed, target/provider/audience-bound handoff."""

    target_host_id: str
    provider: str
    audience: str
    store: CredentialStore

    def __post_init__(self):
        object.__setattr__(self, "target_host_id",
                           _required(self.target_host_id, "target_host_id"))
        object.__setattr__(self, "provider",
                           _required(self.provider, "provider", 80).lower())
        object.__setattr__(self, "audience",
                           _required(self.audience, "audience", 300))

    def install(self, handoff: dict, *, approval_id: str,
                ref: CredentialRef, secret: str) -> dict:
        """Write the local secret and return only non-secret acceptance data."""
        if not isinstance(handoff, dict):
            raise CredentialHandoffInstallError("handoff metadata is invalid")
        approval = _required(approval_id, "approval_id")
        if handoff.get("status") != "consumed":
            raise CredentialHandoffInstallError("handoff is not approval-consumed")
        if (_required(handoff.get("target_host_id"), "target_host_id")
                != self.target_host_id
                or _required(handoff.get("provider"), "provider", 80).lower()
                != self.provider
                or _required(handoff.get("audience"), "audience", 300)
                != self.audience):
            raise CredentialHandoffInstallError("handoff binding mismatch")
        if not isinstance(secret, str) or not secret:
            raise CredentialHandoffInstallError("secret is empty")
        try:
            self.store.write(ref, secret)
        except CredentialStoreError as exc:
            raise CredentialHandoffInstallError(
                "target Keychain write failed; start a new handoff") from exc
        return {
            "accepted": True,
            "handoff_id": _required(handoff.get("handoff_id"), "handoff_id", 120),
            "approval_id": approval,
            "provider": self.provider,
            "credential_ref": ref.as_dict(),
        }
