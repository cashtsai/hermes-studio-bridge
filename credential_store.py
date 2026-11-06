"""Local macOS Keychain boundary for credential handoff targets.

This adapter is intentionally local-only.  The bridge never accepts a
provider secret over the existing metadata handoff endpoint; a target-side
installer or explicitly approved local operation can call this store.  The
``security`` write command receives the secret on stdin, never in argv.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


class CredentialStoreError(RuntimeError):
    """A local credential store operation failed without exposing its value."""


@dataclass(frozen=True)
class CredentialRef:
    service: str
    account: str

    def __post_init__(self):
        for field, value, limit in (
            ("service", self.service, 240), ("account", self.account, 240),
        ):
            if not isinstance(value, str) or not value.strip():
                raise CredentialStoreError(f"{field} is required")
            if len(value) > limit or any(ord(ch) < 0x20 for ch in value):
                raise CredentialStoreError(f"{field} is invalid")

    def as_dict(self) -> dict[str, str]:
        return {"service": self.service, "account": self.account}


class MacOSKeychainStore:
    """Read/write generic-password items without putting secrets in argv."""

    def __init__(self, security_path: str = "/usr/bin/security"):
        self.security_path = security_path

    def _run(self, args: list[str], *, input_text: str | None = None) -> str:
        try:
            result = subprocess.run(
                args, input=input_text, check=True, capture_output=True,
                text=True, timeout=8,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CredentialStoreError("local Keychain operation failed") from exc
        return (result.stdout or "").strip()

    @staticmethod
    def _ref_args(ref: CredentialRef) -> list[str]:
        return ["-s", ref.service, "-a", ref.account]

    def read(self, ref: CredentialRef) -> str:
        value = self._run([self.security_path, "find-generic-password",
                           *self._ref_args(ref), "-w"])
        if not value:
            raise CredentialStoreError("Keychain item is empty")
        return value

    def write(self, ref: CredentialRef, secret: str) -> None:
        if not isinstance(secret, str) or not secret:
            raise CredentialStoreError("secret is empty")
        if len(secret) > 64_000:
            raise CredentialStoreError("secret is too large")
        # `security` prompts for the value when -w is the final option.  The
        # secret travels through stdin and is never visible in process argv.
        self._run([self.security_path, "add-generic-password", "-U",
                   *self._ref_args(ref), "-w"], input_text=secret + "\n")

    def delete(self, ref: CredentialRef) -> None:
        self._run([self.security_path, "delete-generic-password",
                   *self._ref_args(ref)])
