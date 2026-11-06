"""Small APNs private-key provider boundary.

The bridge keeps the historical file path as the default for compatibility.
Operators may opt into macOS Keychain storage with ``APNS_KEY_SOURCE=keychain``
without changing the sender or exposing key material to the process log.
"""

from __future__ import annotations

import os
import stat
import subprocess


class APNSKeyError(RuntimeError):
    """The configured APNs key cannot be read safely."""


def _file_key(path: str) -> str:
    expanded = os.path.abspath(os.path.expanduser(path))
    try:
        info = os.stat(expanded)
    except OSError as exc:
        raise APNSKeyError("APNs key file is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > 16_384:
        raise APNSKeyError("APNs key file is not a valid regular file")
    # Strict mode is opt-in so existing installations do not silently lose
    # pushes during migration. New installs should enable it.
    if os.environ.get("APNS_KEY_REQUIRE_PRIVATE", "0").strip().lower() in {
        "1", "true", "yes", "on"
    } and info.st_mode & 0o077:
        raise APNSKeyError("APNs key file must be owner-only")
    with open(expanded, encoding="utf-8") as handle:
        value = handle.read().strip()
    if not value:
        raise APNSKeyError("APNs key file is empty")
    return value


def _keychain_key(service: str, account: str) -> str:
    if not service or not account:
        raise APNSKeyError("APNs Keychain service/account is missing")
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service,
             "-a", account, "-w"],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise APNSKeyError("APNs Keychain item is unavailable") from exc
    value = (result.stdout or "").strip()
    if not value:
        raise APNSKeyError("APNs Keychain item is empty")
    return value


def read_apns_key(*, source: str | None = None, path: str | None = None,
                  service: str | None = None, account: str | None = None) -> str:
    source = (source or os.environ.get("APNS_KEY_SOURCE", "file")).strip().lower()
    if source == "file":
        return _file_key(path or os.environ.get("APNS_KEY_PATH", ""))
    if source == "keychain":
        return _keychain_key(
            service or os.environ.get("APNS_KEYCHAIN_SERVICE", "pocket.apns"),
            account or os.environ.get("APNS_KEYCHAIN_ACCOUNT", "team-key"),
        )
    raise APNSKeyError("unsupported APNS_KEY_SOURCE")


def available(**kwargs) -> bool:
    try:
        read_apns_key(**kwargs)
        return True
    except APNSKeyError:
        return False
