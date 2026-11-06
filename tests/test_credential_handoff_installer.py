"""Approval-bound, local-only credential installation tests."""

import _isolation  # noqa: F401

import unittest

from credential_handoff_installer import (ApprovedCredentialInstaller,
                                          CredentialHandoffInstallError)
from credential_store import CredentialRef, CredentialStoreError


class FakeStore:
    def __init__(self):
        self.calls = []

    def write(self, ref, secret):
        self.calls.append((ref, secret))


def consumed(**overrides):
    data = {
        "handoff_id": "ho-1",
        "target_host_id": "siege",
        "provider": "codex",
        "audience": "provider-session:thread-1",
        "status": "consumed",
    }
    data.update(overrides)
    return data


class CredentialHandoffInstallerTest(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.installer = ApprovedCredentialInstaller(
            "siege", "codex", "provider-session:thread-1", self.store)
        self.ref = CredentialRef("pocket.provider.codex", "main")

    def test_install_requires_consumed_binding_and_returns_no_secret(self):
        result = self.installer.install(
            consumed(), approval_id="approval-7", ref=self.ref,
            secret="super-secret")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["credential_ref"], self.ref.as_dict())
        self.assertNotIn("super-secret", str(result))
        self.assertEqual(self.store.calls, [(self.ref, "super-secret")])

    def test_issued_ticket_or_missing_approval_is_rejected(self):
        with self.assertRaises(CredentialHandoffInstallError):
            self.installer.install(consumed(status="issued"), approval_id="a",
                                   ref=self.ref, secret="secret")
        with self.assertRaises(CredentialHandoffInstallError):
            self.installer.install(consumed(), approval_id="", ref=self.ref,
                                   secret="secret")

    def test_binding_mismatch_does_not_write(self):
        with self.assertRaises(CredentialHandoffInstallError):
            self.installer.install(consumed(target_host_id="cashcamp"),
                                   approval_id="a", ref=self.ref, secret="secret")
        self.assertEqual(self.store.calls, [])

    def test_store_failure_is_safe(self):
        class BrokenStore(FakeStore):
            def write(self, ref, secret):
                raise CredentialStoreError("do not expose this")

        installer = ApprovedCredentialInstaller(
            "siege", "codex", "provider-session:thread-1", BrokenStore())
        with self.assertRaisesRegex(CredentialHandoffInstallError,
                                    "start a new handoff"):
            installer.install(consumed(), approval_id="a", ref=self.ref,
                              secret="super-secret")


if __name__ == "__main__":
    unittest.main(verbosity=2)
