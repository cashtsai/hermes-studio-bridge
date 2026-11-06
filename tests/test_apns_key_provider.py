import _isolation  # noqa: F401

import os
import tempfile
import unittest
from unittest.mock import patch

from apns_key_provider import APNSKeyError, available, read_apns_key


class APNSKeyProviderTest(unittest.TestCase):
    def test_file_provider_reads_without_logging_or_transforming(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as handle:
            handle.write("-----BEGIN PRIVATE KEY-----\nsecret\n")
            path = handle.name
        try:
            self.assertIn("BEGIN PRIVATE KEY", read_apns_key(source="file", path=path))
            self.assertTrue(available(source="file", path=path))
        finally:
            os.unlink(path)

    def test_strict_file_mode_is_opt_in(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as handle:
            handle.write("key")
            path = handle.name
        try:
            os.chmod(path, 0o644)
            with patch.dict(os.environ, {"APNS_KEY_REQUIRE_PRIVATE": "1"}):
                with self.assertRaises(APNSKeyError):
                    read_apns_key(source="file", path=path)
        finally:
            os.unlink(path)

    def test_keychain_provider_uses_security_without_returning_secret_to_logs(self):
        fake = type("Result", (), {"stdout": "p8-material\n"})()
        with patch("apns_key_provider.subprocess.run", return_value=fake) as run:
            self.assertEqual(read_apns_key(source="keychain", service="svc",
                                           account="acct"), "p8-material")
        run.assert_called_once()
        self.assertNotIn("p8-material", " ".join(run.call_args.args[0]))

    def test_keychain_errors_are_boolean_for_availability(self):
        with patch("apns_key_provider.subprocess.run", side_effect=OSError("no")):
            self.assertFalse(available(source="keychain", service="svc",
                                       account="acct"))


if __name__ == "__main__":
    unittest.main()
