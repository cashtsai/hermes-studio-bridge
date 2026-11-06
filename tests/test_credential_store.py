"""Local Keychain adapter safety tests."""

import _isolation  # noqa: F401

import unittest
from unittest.mock import patch

from credential_store import CredentialRef, CredentialStoreError, MacOSKeychainStore


class CredentialStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = MacOSKeychainStore("/fake/security")
        self.ref = CredentialRef("pocket.provider.codex", "main")

    def test_read_uses_ref_and_returns_value(self):
        with patch("credential_store.subprocess.run",
                   return_value=type("R", (), {"stdout": "value\n"})()) as run:
            self.assertEqual(self.store.read(self.ref), "value")
        args = run.call_args.args[0]
        self.assertEqual(args[-1], "-w")
        self.assertNotIn("value", args)

    def test_write_keeps_secret_out_of_argv(self):
        with patch("credential_store.subprocess.run",
                   return_value=type("R", (), {"stdout": ""})()) as run:
            self.store.write(self.ref, "super-secret")
        args = run.call_args.args[0]
        self.assertNotIn("super-secret", args)
        self.assertEqual(run.call_args.kwargs["input"], "super-secret\n")
        self.assertEqual(args[-1], "-w")

    def test_invalid_ref_and_empty_secret_rejected(self):
        with self.assertRaises(CredentialStoreError):
            CredentialRef("", "main")
        with self.assertRaises(CredentialStoreError):
            self.store.write(self.ref, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
